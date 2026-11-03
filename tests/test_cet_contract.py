#!/usr/bin/env python3
"""四六级听力黑匣子 · 接口契约测试（C 负责）。

同一套断言同时用于 Mock API 与真实后端，只换 base URL。
这是任务书 6.1「D1 第三步 · 5/6」和「D2 · 1」要求的可切换测试脚本。

用法：
    # 打 Mock（默认）
    .venv/bin/python -m pytest tests/test_cet_contract.py -v

    # 打真实服务
    CET_API_BASE=http://127.0.0.1:5173 python -m pytest tests/test_cet_contract.py -v

    # 只跑契约层（不依赖会话状态的纯形状断言）
    CET_API_BASE=http://127.0.0.1:5173 python -m pytest tests/test_cet_contract.py -v -k contract

结果判定口径：
    - Mock 通过 = 契约与测试工具可运行（`CET_API_BASE` 指向 mock 时 pytest 会打印提示）。
    - 真实通过 = 该环境的真实业务行为符合契约。两者含义不同，不要混记。

测试分层：
    contract 层  只依赖 fixtures 与 HTTP 形状，不改服务状态，适合任一环境先跑。
    flow 层      依赖服务端会话状态（交卷→标记→带练→复习），Mock 与真实都要能走通。
    fault 层     依赖场景注入（慢/错误/过期/重复），真实服务上按能力自动跳过。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
import requests

BASE = os.environ.get("CET_API_BASE", "http://127.0.0.1:5199").rstrip("/")
IS_MOCK = "5199" in BASE or "mock" in BASE.lower()
TIMEOUT = float(os.environ.get("CET_HTTP_TIMEOUT", "15"))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cet"
MATERIAL_ID = "cet6-202512-conv2"
MATERIAL_VERSION = "v1"


# ────────────────────────── 基础设施 ──────────────────────────


def fixture(name: str):
    with (FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def api(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    return requests.request(method, f"{BASE}{path}", **kwargs)


def require_service():
    try:
        r = requests.get(f"{BASE}/api/cet/materials", timeout=5)
    except requests.RequestException as exc:
        pytest.skip(f"服务不可达 {BASE}：{exc}")
    if r.status_code >= 500 and not IS_MOCK:
        pytest.skip(f"服务返回 {r.status_code}，可能尚未实现 CET 接口：{r.text[:200]}")


def reset_mock():
    """仅在 Mock 上重置会话状态；真实服务不做破坏性重置。"""
    if IS_MOCK:
        requests.post(f"{BASE}/api/cet/_mock/reset", timeout=5)


@pytest.fixture(autouse=True)
def _service_alive():
    require_service()


@pytest.fixture(scope="module")
def sentences():
    return fixture(f"material_{MATERIAL_ID}.json")["sentences"]


def _unique(prefix: str) -> str:
    """每次运行唯一，避免真实服务上跨轮次数据串扰。"""
    return f"{prefix}-{os.getpid()}-{int(time.time() * 1000) % 10_000_000}"


# ────────────────────────── contract：材料与句子 ──────────────────────────


@pytest.mark.contract
def test_materials_list_shape():
    r = api("GET", "/api/cet/materials")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and isinstance(body["items"], list)
    target = next((m for m in body["items"] if m["materialId"] == MATERIAL_ID), None)
    assert target is not None, f"材料列表缺少 {MATERIAL_ID}"
    for field in ("materialId", "materialVersion", "title", "level", "audioUrl", "durationSec"):
        assert field in target, f"材料缺少必要字段 {field}"
    assert target["level"] in ("cet4", "cet6")


@pytest.mark.contract
def test_material_detail_has_sentences(sentences):
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}", params={"version": MATERIAL_VERSION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "material" in body and "sentences" in body
    got = body["sentences"]
    assert len(got) == len(sentences), f"句子数量不一致：服务 {len(got)}，样例 {len(sentences)}"
    for s in got:
        for field in ("materialId", "materialVersion", "sentenceId", "index", "startSec", "endSec", "text"):
            assert field in s, f"句子缺少字段 {field}：{s}"
        assert s["materialVersion"] == MATERIAL_VERSION


@pytest.mark.contract
def test_sentence_timeline_is_valid(sentences):
    """时间轴合法性：不倒置、不越界、不重叠。文档要求时间用绝对秒数。"""
    duration = fixture(f"material_{MATERIAL_ID}.json")["material"]["durationSec"]
    prev_end = -1.0
    for s in sentences:
        start, end = s["startSec"], s["endSec"]
        assert 0 <= start < end, f"{s['sentenceId']} 时间非法：{start}→{end}"
        assert end <= duration + 0.001, f"{s['sentenceId']} 越界：{end} > {duration}"
        assert start + 0.001 >= prev_end, f"{s['sentenceId']} 与上一句重叠：{prev_end}→{start}"
        assert s["text"].strip(), f"{s['sentenceId']} 原文为空"
        prev_end = end


@pytest.mark.contract
def test_material_version_mismatch_is_rejected():
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}", params={"version": "bogus-v0"})
    assert r.status_code in (409, 422), f"版本不一致应被拒绝，实际 {r.status_code}：{r.text[:200]}"


@pytest.mark.contract
def test_unknown_material_returns_error():
    r = api("GET", "/api/cet/materials/does-not-exist")
    assert r.status_code in (404, 422), r.text
    body = r.json()
    assert "error" in body or "detail" in body, "错误响应应可被页面显示"


@pytest.mark.contract
def test_audio_asset_is_served():
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}/audio")
    if r.status_code == 404 and not IS_MOCK:
        pytest.skip("真实服务未提供音频路由（待 B 实现），Mock 已验证该路径")
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("audio/")
    assert len(r.content) > 100_000, "音频响应体过小"


@pytest.mark.contract
def test_audio_identity_matches_fixture_sha256():
    """音频身份用哈希核对，不用路径。样例音频不入库时这条只在本地有文件时成立。"""
    manifest = fixture("manifest.json")
    expected = manifest["audio"]["sha256"]
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}/audio")
    if r.status_code != 200:
        pytest.skip(f"音频不可取（{r.status_code}），跳过哈希核对")
    got = hashlib.sha256(r.content).hexdigest()
    assert got == expected, f"音频哈希不一致：服务 {got}，样例 {expected}"


@pytest.mark.contract
def test_audio_range_request_supported():
    """播放器拖动进度需要 Range 支持，否则回听定位会失效。"""
    r = api(
        "GET",
        f"/api/cet/materials/{MATERIAL_ID}/audio",
        headers={"Range": "bytes=0-1023"},
    )
    if r.status_code == 404:
        pytest.skip("音频路由不可用")
    assert r.status_code in (206, 200), f"Range 请求返回 {r.status_code}"
    if r.status_code == 206:
        assert "content-range" in {k.lower(): v for k, v in r.headers.items()}


# ────────────────────────── contract：题目与答案可见性 ──────────────────────────


@pytest.mark.contract
def test_questions_hide_answers_before_attempt():
    """任务书 7.3：交卷前不返回参考答案、证据与解释。"""
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions", params={"version": MATERIAL_VERSION})
    assert r.status_code == 200, r.text
    body = r.json()
    items = body.get("items") or body.get("questions")
    assert isinstance(items, list) and items, "题目列表为空"
    for q in items:
        assert "answer" not in q, f"交卷前暴露 answer：{q.get('questionId')}"
        assert "evidenceRef" not in q, f"交卷前暴露 evidenceRef：{q.get('questionId')}"
        assert "explanation" not in q, f"交卷前暴露 explanation：{q.get('questionId')}"
        assert q.get("options"), f"{q.get('questionId')} 无选项"
        for field in ("questionId", "number", "stem"):
            assert field in q, f"题目缺少字段 {field}"


@pytest.mark.contract
def test_question_numbers_are_unique():
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions")
    if r.status_code != 200:
        pytest.skip(r.text[:120])
    items = r.json().get("items", [])
    numbers = [q["number"] for q in items]
    assert len(numbers) == len(set(numbers)), f"题号重复：{numbers}"


# ────────────────────────── flow：交卷 ──────────────────────────


@pytest.mark.flow
def test_attempt_returns_answers_and_evidence():
    reset_mock()
    qs = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()["items"]
    answers = [{"questionId": q["questionId"], "choice": 0} for q in qs]
    r = api(
        "POST",
        "/api/cet/attempts",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "answers": answers,
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("answersVisible") is True, "交卷后应标记答案可见"
    results = body["results"]
    assert len(results) == len(answers)
    for res in results:
        assert "answer" in res, f"交卷后缺少 answer：{res['questionId']}"
        assert "correct" in res, "交卷后应能判对错（未知答案除外）"
        if res.get("answerSource") == "unknown":
            assert res["answer"] is None, "来源为 unknown 时答案必须是 null"
            assert res["correct"] is None, "未知答案不得判对错"


@pytest.mark.flow
def test_attempt_unknown_answer_not_guessed():
    """未知答案保留 unknown：判题返回 null，且不进入正确数。"""
    reset_mock()
    qs = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()["items"]
    unknown = [q for q in qs if q.get("answerSource") == "unknown"]
    if not unknown:
        pytest.skip("该环境没有 unknown 来源题目")
    r = api(
        "POST",
        "/api/cet/attempts",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen-unknown"),
            "answers": [{"questionId": q["questionId"], "choice": 0} for q in unknown],
        },
    )
    assert r.status_code in (200, 201), r.text
    for res in r.json()["results"]:
        assert res["answer"] is None
        assert res["correct"] is None


@pytest.mark.flow
def test_duplicate_attempt_is_idempotent():
    """同一听音会话重复交卷：不新增记录，已提交答案保留。"""
    reset_mock()
    qs = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()["items"]
    payload = {
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "listeningSessionId": _unique("listen-dup"),
        "answers": [{"questionId": q["questionId"], "choice": 1} for q in qs],
    }
    first = api("POST", "/api/cet/attempts", json=payload)
    assert first.status_code in (200, 201), first.text
    second = api("POST", "/api/cet/attempts", json=payload)
    if second.status_code in (200, 201):
        assert second.json().get("attemptId") == first.json().get("attemptId"), \
            "重复交卷不应产生新 attemptId"
    else:
        assert second.status_code in (400, 409, 422), second.text


@pytest.mark.flow
def test_attempt_cannot_change_existing_answers():
    """首次作答保留：换了选项的重复交卷必须被拒绝，不能覆盖。"""
    reset_mock()
    qs = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()["items"]
    session = _unique("listen-lock")
    first = api(
        "POST",
        "/api/cet/attempts",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": session,
            "answers": [{"questionId": q["questionId"], "choice": 0} for q in qs],
        },
    )
    assert first.status_code in (200, 201), first.text
    second = api(
        "POST",
        "/api/cet/attempts",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": session,
            "answers": [{"questionId": q["questionId"], "choice": 2} for q in qs],
        },
    )
    assert second.status_code in (400, 409, 422), (
        f"改动已提交作答应被拒绝，实际 {second.status_code}：{second.text[:200]}"
    )


# ────────────────────────── flow：标记与确认 ──────────────────────────


@pytest.mark.flow
def test_mark_create_and_list(sentences):
    reset_mock()
    session = _unique("listen-mark")
    pressed = 36.9
    r = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": session,
            "pressedAtSec": pressed,
            "eventId": _unique("evt"),
        },
    )
    assert r.status_code in (200, 201), r.text
    mark = r.json()
    assert mark["pressedAtSec"] == pytest.approx(pressed, abs=0.01), "原始按键秒数必须原样保留"
    assert mark.get("suggestedSentenceId"), "应返回建议句子"
    assert mark["materialVersion"] == MATERIAL_VERSION

    listing = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID})
    assert listing.status_code == 200, listing.text
    ids = [m["markId"] for m in listing.json()["items"]]
    assert mark["markId"] in ids, "刚创建的标记未出现在列表里"


@pytest.mark.flow
def test_duplicate_mark_event_does_not_create_record():
    """长按连发 / 网络重试用 eventId 去重，重复操作不产生重复标记。"""
    reset_mock()
    session = _unique("listen-hold")
    event_id = _unique("evt")
    payload = {
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "listeningSessionId": session,
        "pressedAtSec": 42.0,
        "eventId": event_id,
    }
    first = api("POST", "/api/cet/marks", json=payload)
    assert first.status_code in (200, 201), first.text
    second = api("POST", "/api/cet/marks", json=payload)
    assert second.status_code in (200, 201), second.text
    assert second.json()["markId"] == first.json()["markId"], "重复事件不应新增标记"

    listing = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID}).json()["items"]
    same = [m for m in listing if m["listeningSessionId"] == session]
    assert len(same) == 1, f"同一事件应只有 1 条标记，实际 {len(same)}"


@pytest.mark.flow
def test_mark_out_of_range_rejected():
    reset_mock()
    duration = fixture(f"material_{MATERIAL_ID}.json")["material"]["durationSec"]
    r = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": duration + 30,
        },
    )
    assert r.status_code in (400, 422), f"越界按键应被拒绝，实际 {r.status_code}"


@pytest.mark.flow
def test_mark_negative_time_rejected():
    r = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": -1,
        },
    )
    assert r.status_code in (400, 422), f"负时间应被拒绝，实际 {r.status_code}"


@pytest.mark.flow
def test_mark_confirm_and_adjust(sentences):
    """回听确认：可以改成前后句，确认结果是最终绑定依据。"""
    reset_mock()
    created = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": 36.9,
        },
    )
    assert created.status_code in (200, 201), created.text
    mark = created.json()
    assert mark["confirmedSentenceId"] in (None, ""), "新建标记不应已确认句子"

    # 用户选择后一句
    sug_idx = next(i for i, s in enumerate(sentences) if s["sentenceId"] == mark["suggestedSentenceId"])
    target = sentences[min(sug_idx + 1, len(sentences) - 1)]["sentenceId"]
    r = api("PATCH", f"/api/cet/marks/{mark['markId']}", json={"confirmedSentenceId": target})
    assert r.status_code == 200, r.text
    assert r.json()["confirmedSentenceId"] == target
    assert r.json().get("confirmedAtSec"), "确认后应记录确认时间"


@pytest.mark.flow
def test_mark_confirm_invalid_sentence_rejected():
    """非法句子引用必须报错并指出句子，不能静默接受。"""
    reset_mock()
    created = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": 36.9,
        },
    )
    assert created.status_code in (200, 201), created.text
    mark_id = created.json()["markId"]
    r = api("PATCH", f"/api/cet/marks/{mark_id}", json={"confirmedSentenceId": "s-not-exist"})
    assert r.status_code in (400, 404, 422), f"非法句子应被拒绝，实际 {r.status_code}"
    assert "s-not-exist" in r.text, "错误信息应指出具体句子"


@pytest.mark.flow
def test_marks_isolated_by_material():
    reset_mock()
    api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": 20.0,
        },
    )
    r = api("GET", "/api/cet/marks", params={"materialId": "other-material"})
    assert r.status_code in (200, 404, 422), r.text
    if r.status_code == 200:
        for m in r.json()["items"]:
            assert m["materialId"] == "other-material", "标记未按材料隔离"


# ────────────────────────── flow：带练、反馈与计时 ──────────────────────────


def _create_confirmed_mark():
    created = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": 36.9,
        },
    )
    assert created.status_code in (200, 201), created.text
    mark = created.json()
    r = api("PATCH", f"/api/cet/marks/{mark['markId']}", json={"confirmedSentenceId": mark["suggestedSentenceId"]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.flow
def test_practice_requires_confirmed_mark():
    """未确认句子就建练习应被拒绝，避免带练绑错句子。"""
    reset_mock()
    created = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": 60.0,
        },
    )
    assert created.status_code in (200, 201), created.text
    r = api("POST", "/api/cet/practices", json={"markId": created.json()["markId"]})
    assert r.status_code in (400, 409, 422), f"未确认句子应拒绝建练习，实际 {r.status_code}"


@pytest.mark.flow
def test_practice_create_and_restore_same_id():
    """刷新/重启后按原 practiceId 恢复同一练习，不新建。"""
    reset_mock()
    mark = _create_confirmed_mark()
    created = api("POST", "/api/cet/practices", json={"markId": mark["markId"]})
    assert created.status_code in (200, 201), created.text
    practice = created.json()
    pid = practice["practiceId"]
    assert practice["sentenceId"] == mark["confirmedSentenceId"], "练习应绑定确认后的句子"
    assert practice["materialVersion"] == MATERIAL_VERSION

    again = api("POST", "/api/cet/practices", json={"markId": mark["markId"]})
    assert again.status_code in (200, 201), again.text
    assert again.json()["practiceId"] == pid, "同一标记重复创建应返回原练习"

    restored = api("GET", f"/api/cet/practices/{pid}")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["practiceId"] == pid
    assert body["sentenceId"] == practice["sentenceId"]
    assert body.get("currentStep") is not None, "恢复时应返回当前步骤"


@pytest.mark.flow
def test_feedback_leads_to_different_actions():
    """没听清（sound）与不懂意思（meaning）必须进入不同帮助路径。"""
    reset_mock()
    mark = _create_confirmed_mark()
    practice = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()
    pid = practice["practiceId"]

    r_sound = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "sound"})
    assert r_sound.status_code == 200, r_sound.text
    step_sound = r_sound.json()["step"]

    r_meaning = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "meaning"})
    assert r_meaning.status_code == 200, r_meaning.text
    step_meaning = r_meaning.json()["step"]

    assert step_sound["action"] != step_meaning["action"], (
        f"两类反馈应走不同动作，实际都是 {step_sound['action']}"
    )
    # 没听清应先慢放；不懂意思应先解释
    assert step_sound["action"] in ("slow", "chunk"), f"sound 走了 {step_sound['action']}"
    assert step_meaning["action"] in ("explain",), f"meaning 走了 {step_meaning['action']}"
    if step_sound.get("playback"):
        assert step_sound["playback"]["speed"] < 1.0, "慢放动作速度应小于 1.0"
    assert step_meaning.get("textVisible") is True, "解释动作应显示文字"


@pytest.mark.flow
def test_heard_feedback_hides_text_and_plays_original_speed():
    """看懂后：隐藏文字、原速回听。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    r = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "heard"})
    assert r.status_code == 200, r.text
    step = r.json()["step"]
    assert step.get("textVisible") is False, "heard 后必须隐藏文字"
    if step.get("playback"):
        assert step["playback"]["speed"] == pytest.approx(1.0), "heard 后应原速回听"


@pytest.mark.flow
def test_stale_step_result_is_rejected():
    """迟到的模型结果不能覆盖当前步骤（任务书 T09）。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    first = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "sound"}).json()
    old_step_id = first["step"]["stepId"]
    # 用户切到下一步
    api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "meaning"})
    # 旧步骤的迟到结果
    late = api(
        "POST",
        f"/api/cet/practices/{pid}/next",
        json={"feedback": "sound", "stepId": old_step_id},
    )
    assert late.status_code in (409, 422), (
        f"基于旧步骤的请求应被拒绝，实际 {late.status_code}：{late.text[:200]}"
    )


@pytest.mark.flow
def test_local_help_works_without_model():
    """模型不可用时本地动作仍可用（T08）。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    for action in ("replay", "slow", "chunk", "explain"):
        r = api("POST", f"/api/cet/practices/{pid}/local", json={"action": action})
        assert r.status_code == 200, f"本地动作 {action} 失败：{r.text[:200]}"
        body = r.json()
        assert body["step"]["action"] == action
        assert body["step"].get("playback") or body["step"].get("textVisible"), \
            f"本地动作 {action} 没有任何可播放/可显示内容"


@pytest.mark.flow
def test_playback_events_dedupe_and_count_time():
    """重复上报不重复计时；只有前台可见的播放累计有效时长。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", f"/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    event_id = _unique("pev")
    payload = {
        "eventId": event_id,
        "type": "end",
        "stepId": "step-x",
        "playedMs": 3000,
        "visible": True,
    }
    first = api("POST", f"/api/cet/practices/{pid}/events", json=payload)
    assert first.status_code == 200, first.text
    after_first = first.json()["effectiveMs"]
    assert after_first >= 3000, f"前台播放应计时，实际 {after_first}"

    second = api("POST", f"/api/cet/practices/{pid}/events", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["effectiveMs"] == after_first, "重复事件不应重复计时"

    # 后台播放不计时
    bg = api(
        "POST",
        f"/api/cet/practices/{pid}/events",
        json={"eventId": _unique("pev"), "type": "end", "playedMs": 5000, "visible": False},
    )
    assert bg.status_code == 200, bg.text
    assert bg.json()["effectiveMs"] == after_first, "后台播放不应累计有效时长"


@pytest.mark.flow
def test_invalid_event_type_rejected():
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    r = api(
        "POST",
        f"/api/cet/practices/{pid}/events",
        json={"eventId": _unique("pev"), "type": "bogus", "playedMs": 100},
    )
    assert r.status_code in (400, 422), r.text


# ────────────────────────────────────────── flow：卡点与复习 ──────────────────────────────


def _finish_practice(mark, feedback: str) -> dict:
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": feedback})
    r = api("POST", f"/api/cet/practices/{pid}/finish", json={"feedback": feedback})
    assert r.status_code == 200, r.text
    return r.json()


def _make_mark_at(pressed: float) -> dict:
    created = api(
        "POST",
        "/api/cet/marks",
        json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique("listen"),
            "pressedAtSec": pressed,
        },
    )
    assert created.status_code in (200, 201), created.text
    m = created.json()
    r = api("PATCH", f"/api/cet/marks/{m['markId']}", json={"confirmedSentenceId": m["suggestedSentenceId"]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.flow
def test_finish_is_idempotent_and_latest_feedback_wins():
    """重复保存不新增记录；早先听懂后来说没听清，最终结果取最新。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    # 用户先自评听懂
    api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "heard"})
    # 后来又反馈没听清
    api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "sound"})
    r1 = api("POST", f"/api/cet/practices/{pid}/finish", json={"feedback": "sound"})
    assert r1.status_code == 200, r1.text
    assert r1.json().get("latestFeedback") == "sound", "最终结果必须反映最新反馈 sound"
    r2 = api("POST", f"/api/cet/practices/{pid}/finish", json={"feedback": "sound"})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("idempotent") is True or r2.json()["status"] == "complete"


@pytest.mark.flow
def test_review_items_reflect_latest_feedback():
    reset_mock()
    mark = _make_mark_at(36.9)
    _finish_practice(mark, "sound")
    r = api("GET", "/api/cet/review-items", params={"materialId": MATERIAL_ID})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "完成的练习应出现在卡点列表"
    assert any(i["latestFeedback"] == "sound" for i in items)


@pytest.mark.flow
def test_review_session_binds_original_practice_id():
    """复习恢复必须用原练习 ID（任务书 T12 的核心）。"""
    reset_mock()
    m1 = _make_mark_at(36.9)
    m2 = _make_mark_at(86.0)
    finished = [_finish_practice(m1, "sound"), _finish_practice(m2, "meaning")]
    original_ids = {f["practiceId"] for f in finished}

    created = api("POST", "/api/cet/reviews", json={"maxItems": 3, "budgetMs": 600_000})
    assert created.status_code in (200, 201), created.text
    review = created.json()
    assert review["budgetMs"] == 600_000
    item_pids = {i["practiceId"] for i in review["items"]}
    assert item_pids <= original_ids, f"复习条目绑定了不存在的练习：{item_pids - original_ids}"
    assert item_pids == original_ids, f"两处卡点都应进入复习，实际 {item_pids}"

    # 暂停 → 重启后按同一 reviewId 恢复
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "pause"})
    restored = api("GET", f"/api/cet/reviews/{rid}")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["status"] == "paused"
    assert {i["practiceId"] for i in body["items"]} == item_pids, "恢复后练习 ID 变了"


@pytest.mark.flow
def test_review_skip_keeps_item_pending():
    """跳过 ≠ 听懂：跳过条目必须保留为待练（T11）。"""
    reset_mock()
    m1 = _make_mark_at(36.9)
    m2 = _make_mark_at(86.0)
    _finish_practice(m1, "sound")
    _finish_practice(m2, "meaning")
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    first_item = review["items"][0]["itemId"]
    r = api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "skip", "itemId": first_item})
    assert r.status_code == 200, r.text
    body = r.json()
    skipped = next(i for i in body["items"] if i["itemId"] == first_item)
    assert skipped["status"] != "done", "跳过的条目不能标记为完成"
    assert "skip" in skipped["status"] or skipped["status"] == "pending", \
        f"跳过条目应保持待练，实际 {skipped['status']}"


@pytest.mark.flow
def test_review_pause_does_not_accumulate_time():
    """暂停期间不累计有效用时；只有 active 且前台才计时。"""
    reset_mock()
    m1 = _make_mark_at(36.9)
    _finish_practice(m1, "sound")
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    t1 = api("POST", f"/api/cet/reviews/{rid}/actions",
             json={"action": "tick", "elapsedMs": 5000, "visible": True}).json()
    assert t1["effectiveMs"] == 5000, f"active 时应计时，实际 {t1['effectiveMs']}"
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "pause"})
    t2 = api("POST", f"/api/cet/reviews/{rid}/actions",
             json={"action": "tick", "elapsedMs": 60000, "visible": True}).json()
    assert t2["effectiveMs"] == 5000, f"暂停期间不应计时，实际 {t2['effectiveMs']}"
    # 后台活动也不计
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "resume"})
    t3 = api("POST", f"/api/cet/reviews/{rid}/actions",
             json={"action": "tick", "elapsedMs": 4000, "visible": False}).json()
    assert t3["effectiveMs"] == 5000, f"后台活动不应计时，实际 {t3['effectiveMs']}"


# ────────────────────────── fault：场景注入 ──────────────────────────


@pytest.mark.fault
def test_error_scenario_returns_actionable_error():
    r = api("GET", "/api/cet/materials", params={"scenario": "error"})
    if r.status_code == 200:
        pytest.skip("该服务不支持故障注入（真实后端预期行为）")
    assert r.status_code >= 500, r.text
    body = r.json()
    info = body.get("error_info") or {}
    assert info.get("code"), "错误应带可判定 code"
    assert info.get("action") or info.get("message"), "错误应可被页面显示给用户"


@pytest.mark.fault
def test_slow_scenario_is_slow():
    start = time.time()
    r = api("GET", "/api/cet/materials", params={"scenario": "slow"})
    elapsed = time.time() - start
    if r.status_code != 200:
        pytest.skip("该服务不支持慢响应注入")
    assert elapsed >= 1.5, f"慢响应场景应明显变慢，实际 {elapsed:.2f}s"


@pytest.mark.fault
def test_empty_scenario_returns_empty_list():
    r = api("GET", "/api/cet/materials", params={"scenario": "empty"})
    if r.status_code != 200:
        pytest.skip("该服务不支持空列表注入")
    body = r.json()
    if body.get("count") != 0:
        pytest.skip("该服务不支持空列表注入（返回了真实数据）")
    assert body["items"] == []


# ────────────────────────── 运行提示 ──────────────────────────


def test_report_target_environment():
    """把当前测试目标写进输出，避免把 Mock 结果误记为真实验收。"""
    info = requests.get(f"{BASE}/api/cet/_mock/info", timeout=3) if IS_MOCK else None
    if info is not None and info.status_code == 200:
        print(f"\n[目标环境] Mock API {BASE}（契约通过，不代表真实业务正确）")
    else:
        print(f"\n[目标环境] 真实/未知服务 {BASE}（结果计为真实服务契约检查）")
