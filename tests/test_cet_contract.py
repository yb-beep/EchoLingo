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
import threading
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

# 任务书 7.3 逐条规定的枚举值。断言用「必须落在集合内」而不是「包含某子串」——
# 后者太松：C 曾自造 skipped_pending 并通过了 "skip" in status 的断言，
# 而契约要求的是 skipped。松断言会把契约违规放过去。
CONTRACT_ENUMS = {
    "feedback": {"sound", "meaning", "heard"},
    "stepAction": {"replay", "slow", "chunk", "explain", "transfer", "finish"},
    "practiceStatus": {"active", "complete"},
    "reviewItemStatus": {"pending", "current", "done", "skipped"},
    "reviewStatus": {"ready", "active", "paused", "complete"},
    # 任务书 7.2 末句：「异步模型操作需要可查询状态或可取消的实现」
    "aiRequestStatus": {"idle", "pending", "complete", "failed", "cancelled", "invalid"},
}


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


# 句数基准的已知分歧（问题清单 OBS-002）：
#   底座 Whisper 对同一段音频切 37 句，C 的契约样例固定 32 句。
#   两者文字一致、覆盖同一音频，只是句边界粒度不同。
# 处理原则：
#   - 打 Mock 时：必须与样例**逐字一致**（Mock 就是样例的替身，不容偏差）
#   - 打真实服务时：允许 B 采用不同切分口径，但**必须显式报告**
#     句数差异与覆盖范围，而不是静默通过 —— 否则前端会按错误基准实现。
KNOWN_SENTENCE_COUNT_DEVIATION = {
    "issue": "OBS-002",
    "baseWhisperCount": 37,
    "contractCount": 32,
    "note": "底座更细；A/B 需选同一套基准",
}


@pytest.mark.contract
def test_material_detail_has_sentences(sentences):
    r = api("GET", f"/api/cet/materials/{MATERIAL_ID}", params={"version": MATERIAL_VERSION})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "material" in body and "sentences" in body
    got = body["sentences"]

    for s in got:
        for field in ("materialId", "materialVersion", "sentenceId", "index", "startSec", "endSec", "text"):
            assert field in s, f"句子缺少字段 {field}：{s}"
        assert s["materialVersion"] == MATERIAL_VERSION

    if IS_MOCK:
        # Mock 必须与样例逐字一致
        assert len(got) == len(sentences), (
            f"Mock 句数与样例不一致：服务 {len(got)}，样例 {len(sentences)}"
        )
        return

    # 真实服务：允许切分口径不同，但必须能核对「同一段音频、覆盖完整」
    if len(got) != len(sentences):
        dev = KNOWN_SENTENCE_COUNT_DEVIATION
        print(
            f"\n[句数偏差] 服务 {len(got)} 句 vs 契约 {len(sentences)} 句"
            f"（已知 {dev['issue']}：底座 Whisper 约 {dev['baseWhisperCount']} 句，更细）"
        )
        # 无论切法如何，必须覆盖同一音频范围，且时间轴自洽
        assert got, "句子列表为空"
        first_start = min(float(s["startSec"]) for s in got)
        last_end = max(float(s["endSec"]) for s in got)
        contract_duration = fixture(f"material_{MATERIAL_ID}.json")["material"]["durationSec"]
        assert first_start <= 1.0, f"首句起点 {first_start:.2f}s 偏离音频开头"
        assert last_end >= contract_duration - 1.0, (
            f"末句终点 {last_end:.2f}s 未覆盖到音频结尾 {contract_duration:.2f}s"
        )


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
    # playedMs 必须达到「播完门槛」（句子 s13 约 4.6s，80% ≈ 3.7s），
    # 否则会被正确地判为未播完、不计时 —— 见 test_playback_under_threshold_not_counted。
    payload = {
        "eventId": event_id,
        "type": "end",
        "stepId": "step-x",
        "playedMs": 4200,
        "visible": True,
    }
    first = api("POST", f"/api/cet/practices/{pid}/events", json=payload)
    assert first.status_code == 200, first.text
    after_first = first.json()["effectiveMs"]
    assert after_first >= 4200, f"前台播放应计时，实际 {after_first}"

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
    assert skipped["status"] == "skipped", (
        f"任务书 7.3 规定条目状态只有 pending/current/done/skipped，"
        f"跳过应落 skipped，实际 {skipped['status']!r}"
    )
    # 「跳过 ≠ 听懂」的语义要单独表达，不能靠状态值承载两层含义
    assert skipped.get("needsPractice") is True, (
        "跳过只表示这次不听，条目仍需练习，应带 needsPractice=true"
    )


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


@pytest.mark.flow
def test_transfer_returns_real_candidates():
    """换句检查（T10）：有候选时返回可播放的真实原声引用。

    注意：候选结构目前是 C 提议的形状，字段名待 B 确认。
    这条测试断言的是「有候选时必须给出可定位的音频范围与来源」，
    不断言具体字段名，避免把 C 的提议锁死成契约。
    """
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices",
              json={"markId": mark["markId"], "transferCase": "with_candidates"}).json()["practiceId"]
    r = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "heard"})
    assert r.status_code == 200, r.text
    step = r.json()["step"]
    assert step["action"] == "transfer", f"heard 后应进入换句检查，实际 {step['action']}"

    ref = step.get("candidateRef")
    assert ref, "换句检查必须给出候选引用，不能是 null"
    items = ref.get("items") if isinstance(ref, dict) else ref
    assert items, "有候选场景 items 不能为空"
    for c in items:
        # 候选必须是真实原声：有可播放范围，而不是纯文本
        assert c.get("startSec") is not None and c.get("endSec") is not None, \
            f"候选 {c.get('candidateId')} 缺少可播放范围"
        assert c["startSec"] < c["endSec"], f"候选 {c.get('candidateId')} 时间倒置"
        assert c.get("source"), f"候选 {c.get('candidateId')} 缺少来源"


@pytest.mark.flow
def test_transfer_no_candidate_still_saves():
    """换句检查无候选（T10）：页面走空态，但练习仍可保存卡点，不阻断流程。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices",
              json={"markId": mark["markId"], "transferCase": "no_candidate"}).json()["practiceId"]
    r = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "heard"})
    assert r.status_code == 200, r.text
    step = r.json()["step"]
    ref = step.get("candidateRef") or {}
    items = ref.get("items") if isinstance(ref, dict) else ref
    assert not items, "无候选场景 items 应为空，不能编造候选"

    # 无候选不得阻断保存
    fin = api("POST", f"/api/cet/practices/{pid}/finish", json={"feedback": "heard"})
    assert fin.status_code == 200, f"无候选时应仍可保存卡点：{fin.text[:200]}"
    assert fin.json()["status"] == "complete"


# ────────────────────────── concurrent：并发与一致性 ──────────────────────────
# 任务书 D3 第 1 条要求「检查重启、并发请求和重复操作后的数据一致性」。
# 并发问题的特征是「平时都对，压一下就错」，只靠串行测试永远测不出来，
# 所以单列一层。Mock 内存态与真实数据库都可能在这里暴露问题。


@pytest.mark.concurrent
def test_concurrent_duplicate_events_count_once():
    """同一 eventId 并发上报多次，有效时长只能算一次（防重复计时）。"""
    from concurrent.futures import ThreadPoolExecutor

    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    event_id = _unique("pev-concurrent")
    payload = {
        "eventId": event_id,
        "type": "end",
        "stepId": "step-c",
        "playedMs": 4000,
        "visible": True,
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: api("POST", f"/api/cet/practices/{pid}/events", json=payload), range(8)))

    codes = [r.status_code for r in results]
    assert all(c == 200 for c in codes), f"并发重复事件应全部 200（幂等），实际 {codes}"

    final = api("GET", f"/api/cet/practices/{pid}").json()
    assert final["effectiveMs"] == 4000, (
        f"8 次并发重复上报只能计一次 4000ms，实际 {final['effectiveMs']}ms"
    )


@pytest.mark.concurrent
def test_concurrent_attempts_create_one_record():
    """同一听音会话并发交卷，只能产生一个 attemptId（防重复记录）。"""
    from concurrent.futures import ThreadPoolExecutor

    reset_mock()
    qs = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()["items"]
    session = _unique("listen-concurrent")
    payload = {
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "listeningSessionId": session,
        "answers": [{"questionId": q["questionId"], "choice": 0} for q in qs],
    }

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: api("POST", "/api/cet/attempts", json=payload), range(6)))

    ok = [r for r in results if r.status_code in (200, 201)]
    assert ok, f"并发交卷至少应有一个成功，实际状态码 {[r.status_code for r in results]}"
    ids = {r.json().get("attemptId") for r in ok}
    assert len(ids) == 1, f"同一会话并发交卷应只有一个 attemptId，实际 {ids}"


@pytest.mark.concurrent
def test_concurrent_marks_from_different_events_all_recorded():
    """不同 eventId 的并发标记都要落库，且每个都有唯一 markId（防丢失/防串号）。"""
    from concurrent.futures import ThreadPoolExecutor

    reset_mock()
    session = _unique("listen-multi")

    def one(i: int):
        return api(
            "POST",
            "/api/cet/marks",
            json={
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "listeningSessionId": session,
                "pressedAtSec": 20.0 + i * 5,
                "eventId": _unique(f"evt-{i}"),
            },
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, range(6)))

    assert all(r.status_code in (200, 201) for r in results),         f"并发建不同标记应全部成功，实际 {[r.status_code for r in results]}"
    ids = {r.json()["markId"] for r in results}
    assert len(ids) == 6, f"6 个不同标记应有 6 个唯一 markId，实际 {len(ids)}"

    listing = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID}).json()["items"]
    mine = [m for m in listing if m["listeningSessionId"] == session]
    assert len(mine) == 6, f"列表应有 6 条，实际 {len(mine)}"


@pytest.mark.concurrent
def test_concurrent_stale_and_fresh_next_do_not_corrupt_state():
    """并发请求 next 时，最终状态必须自洽：currentStepId 指向真实存在的步骤。"""
    from concurrent.futures import ThreadPoolExecutor

    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    def one(feedback: str):
        return api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": feedback})

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, ["sound", "meaning", "heard", "sound", "meaning", "heard"]))

    final = api("GET", f"/api/cet/practices/{pid}")
    assert final.status_code == 200, final.text
    body = final.json()
    cur = body.get("currentStep")
    assert cur is not None, "并发后 currentStep 不能为空"
    assert body["currentStepId"] == cur["stepId"], "currentStepId 必须与当前步骤一致"
    assert cur.get("action") in ("replay", "slow", "chunk", "explain", "transfer"),         f"当前步骤动作非法：{cur.get('action')}"


# ────────────────────────── fixture：Mock 与契约样例一致性 ──────────────────────────
# 任务书明确警告：「自己编写接口实现和断言时，还需按团队契约核对，
# 两者一致本身不足以证明真实后端正确。」
# 这条测试防的是更隐蔽的一种情况：Mock 悄悄偏离了契约样例，
# 于是 Mock 测试全绿，但 A 照着样例写前端反而对不上。
# 只在 Mock 上跑 —— 真实服务本就不该与开发样例逐字相同。


@pytest.mark.contract
@pytest.mark.skipif(not IS_MOCK, reason="只校验 Mock 是否忠实实现契约样例")
def test_mock_matches_fixture_material_list():
    mock = api("GET", "/api/cet/materials").json()
    fixture_doc = fixture("materials.json")

    mock_item = next((m for m in mock["items"] if m["materialId"] == MATERIAL_ID), None)
    fix_item = next((m for m in fixture_doc["items"] if m["materialId"] == MATERIAL_ID), None)
    assert mock_item and fix_item, "两边都应包含样例材料"

    # 契约里定义的字段必须逐一相同；Mock 可多返回字段，但不能少或改值
    missing = [k for k in fix_item if k not in mock_item]
    assert not missing, f"Mock 缺少契约字段：{missing}"
    mismatched = {
        k: (fix_item[k], mock_item[k])
        for k in fix_item
        if k != "developmentFixture" and fix_item[k] != mock_item[k]
    }
    assert not mismatched, f"Mock 与契约样例取值不一致：{mismatched}"


@pytest.mark.contract
@pytest.mark.skipif(not IS_MOCK, reason="只校验 Mock 是否忠实实现契约样例")
def test_mock_questions_hide_same_fields_as_fixture():
    """交卷前字段集合必须与样例完全一致 —— 多返回 answer 就是泄题。"""
    mock = api("GET", f"/api/cet/materials/{MATERIAL_ID}/questions").json()
    fix_doc = fixture("questions_before_attempt.json")

    mock_by_id = {q["questionId"]: q for q in mock["items"]}
    for fq in fix_doc["items"]:
        mq = mock_by_id.get(fq["questionId"])
        assert mq is not None, f"Mock 缺少题目 {fq['questionId']}"
        # 样例里没有的敏感字段，Mock 也不能有
        for banned in ("answer", "evidenceRef", "explanation"):
            if banned not in fq:
                assert banned not in mq, (
                    f"契约样例刻意不含 {banned}，但 Mock 返回了它（题 {fq['questionId']}）"
                )


# ────────────────────────── contract：状态枚举合规 ──────────────────────────
# 任务书 7.3 逐条规定了状态值。C 曾自造 skipped_pending，因为断言写成
# "skip" in status 而漏过 —— 松断言会让契约违规悄悄通过。
# 这里把「枚举必须落在集合内」变成系统检查，覆盖所有相关字段，不只一条路径。


@pytest.mark.contract
def test_practice_status_enum():
    reset_mock()
    mark = _create_confirmed_mark()
    created = api("POST", "/api/cet/practices", json={"markId": mark["markId"]})
    assert created.status_code in (200, 201), created.text
    status = created.json()["status"]
    assert status in CONTRACT_ENUMS["practiceStatus"], (
        f"练习状态 {status!r} 不在契约枚举 {sorted(CONTRACT_ENUMS['practiceStatus'])} 内"
    )
    finished = api("POST", f"/api/cet/practices/{created.json()['practiceId']}/finish",
                   json={"feedback": "sound"})
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] in CONTRACT_ENUMS["practiceStatus"]


@pytest.mark.contract
def test_step_action_enum():
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    seen = set()
    for feedback in ("sound", "meaning", "heard"):
        r = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": feedback})
        assert r.status_code == 200, r.text
        action = r.json()["step"]["action"]
        assert action in CONTRACT_ENUMS["stepAction"], (
            f"带练动作 {action!r} 不在契约枚举内"
        )
        seen.add(action)
    assert len(seen) >= 2, f"三类反馈应产生不同动作，实际只有 {seen}"

    for action in ("replay", "slow", "chunk", "explain"):
        r = api("POST", f"/api/cet/practices/{pid}/local", json={"action": action})
        assert r.status_code == 200, r.text
        got = r.json()["step"]["action"]
        assert got in CONTRACT_ENUMS["stepAction"], f"本地动作 {got!r} 不在契约枚举内"


@pytest.mark.contract
def test_review_status_enum_through_lifecycle():
    """复习会话走完 ready→active→paused→active→complete，每步都要合枚举。"""
    reset_mock()
    m1 = _make_mark_at(36.9)
    _finish_practice(m1, "sound")
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]

    assert review["status"] in CONTRACT_ENUMS["reviewStatus"], review["status"]
    for action, expected in (("start", "active"), ("pause", "paused"), ("resume", "active")):
        r = api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": action})
        assert r.status_code == 200, f"{action}: {r.text}"
        got = r.json()["status"]
        assert got in CONTRACT_ENUMS["reviewStatus"], f"{action} 后状态 {got!r} 不合枚举"
        assert got == expected, f"{action} 后应为 {expected}，实际 {got}"


@pytest.mark.contract
def test_review_item_status_always_in_enum():
    """任何操作后，所有条目状态都必须落在契约枚举内。"""
    reset_mock()
    m1 = _make_mark_at(36.9)
    m2 = _make_mark_at(86.0)
    _finish_practice(m1, "sound")
    _finish_practice(m2, "meaning")
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})

    def check_all(payload, stage):
        for item in payload["items"]:
            st = item["status"]
            assert st in CONTRACT_ENUMS["reviewItemStatus"], (
                f"{stage} 后条目 {item['itemId']} 状态 {st!r} 不在契约枚举内"
            )

    check_all(review, "创建")
    first = review["items"][0]["itemId"]
    second = review["items"][1]["itemId"]
    check_all(api("POST", f"/api/cet/reviews/{rid}/actions",
                  json={"action": "skip", "itemId": first}).json(), "跳过")
    check_all(api("POST", f"/api/cet/reviews/{rid}/actions",
                  json={"action": "done", "itemId": second}).json(), "完成")


@pytest.mark.contract
def test_playback_event_carries_required_fields():
    """任务书 7.3：播放事件至少含开始/结束/中断，并带材料版本、练习 ID、
    步骤 ID、事件 ID、音频时间和速度。

    这里验「三件套」：三种类型都可上报、每个字段都被接收、非法类型被拒。
    注意：事件是「写入后才查」的数据，不能只看响应体 —— 有的实现响应精简，
    字段其实存下来了。所以用「同类事件重复上报时返回同一结果」间接确认存储，
    并对必填字段做存在性断言。
    """
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    # 7.3 要求三种事件类型都能处理
    for etype in ("start", "end", "interrupt"):
        r = api("POST", f"/api/cet/practices/{pid}/events", json={
            "eventId": _unique(f"evt-{etype}"),
            "type": etype,
            "stepId": "step-probe",
            "materialVersion": MATERIAL_VERSION,
            "playedMs": 1500 if etype != "start" else 0,
            "audioSec": 38.5,
            "speed": 1.0,
            "visible": True,
        })
        assert r.status_code == 200, f"{etype} 事件应被接受：{r.text[:200]}"
        body = r.json()
        assert "eventId" in body, "响应应回显 eventId（7.3 要求事件带 ID）"

    # 非法类型必须被拒，不能静默接受
    bad = api("POST", f"/api/cet/practices/{pid}/events", json={
        "eventId": _unique("evt-bad"), "type": "pause", "playedMs": 100,
    })
    assert bad.status_code in (400, 422), (
        f"7.3 只定义 start/end/interrupt，'pause' 应被拒，实际 {bad.status_code}"
    )


@pytest.mark.contract
def test_model_request_bound_to_practice_and_step():
    """任务书 7.3：每次模型请求绑定练习 ID、步骤 ID 与当前版本。

    C 的实现用「旧 stepId 请求必须被拒」来落实这一条，
    这里验的是正向：正常请求的返回要带可追溯的 requestId，
    且响应里的步骤属于该练习。
    """
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    r = api("POST", f"/api/cet/practices/{pid}/next", json={"feedback": "meaning"})
    assert r.status_code == 200, r.text
    body = r.json()

    req = body.get("modelRequest") or {}
    assert req.get("requestId"), "模型请求应带可追溯的 requestId（7.3 要求绑定请求）"
    step = body["step"]
    assert step["practiceId"] == pid, "步骤必须绑定到发起请求的练习"
    assert step.get("stepId"), "步骤应有 stepId（7.3 要求绑定步骤 ID）"
    # 过期校验依赖 stepId，缺了它就无法判断迟到结果
    assert "modelRequestId" in step, "步骤应记录它来自哪个模型请求"


@pytest.mark.contract
def test_material_ref_press_time_confirmed_sentence_all_kept():
    """任务书 7.3：材料引用、原始按键时间、确认句子和答案来源分别保留。

    这四项不能合并成一个字段 —— 合并后就无法区分「用户按在哪」
    与「系统吸附到哪」，回听偏差也无从追查。
    """
    reset_mock()
    created = api("POST", "/api/cet/marks", json={
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "listeningSessionId": _unique("listen-four"),
        "pressedAtSec": 36.9,
    })
    assert created.status_code in (200, 201), created.text
    mark = created.json()

    assert mark.get("materialId") == MATERIAL_ID, "材料引用必须保留"
    assert mark.get("materialVersion"), "材料版本必须保留（7.3 事件需带版本）"
    assert mark.get("pressedAtSec") == pytest.approx(36.9, abs=0.01), "原始按键时间必须保留"
    assert mark.get("suggestedSentenceId"), "建议句子必须保留"
    assert mark.get("confirmedSentenceId") in (None, ""), "确认句子此时还未确认"

    confirmed = api("PATCH", f"/api/cet/marks/{mark['markId']}",
                    json={"confirmedSentenceId": mark["suggestedSentenceId"]})
    assert confirmed.status_code == 200, confirmed.text
    after = confirmed.json()
    # 确认后：原始按键时间与建议句子都不能被覆盖掉
    assert after["pressedAtSec"] == pytest.approx(36.9, abs=0.01), \
        "确认句子后原始按键时间不能被改写"
    assert after["suggestedSentenceId"] == mark["suggestedSentenceId"], \
        "确认句子后建议句子不能丢失"
    assert after["confirmedSentenceId"] == mark["suggestedSentenceId"]


# ────────────────────────── robustness：错误形状与畸形输入 ──────────────────────────
# A 写错误处理时最容易踩的坑：错误响应有两种形状。
#   业务校验 -> {"error": "...", "error_info": {code,title,action,...}}
#   请求体解析失败 -> {"detail": [...]}   （FastAPI 默认）
# 底座既有前端用的是 payload.detail || payload.error_info || payload，
# 说明两种都得处理。这里把它固化成契约，避免 A 只处理一种而在另一种上白屏。


@pytest.mark.contract
def test_error_response_is_parseable_by_one_rule():
    """所有错误响应都应能被同一条规则解析出「能显示给用户的信息」。

    规则：detail || error_info || error，任取其一都必须能拿到字符串或可读结构，
    不允许出现「三种都没有」的裸错误体。
    """
    cases = [
        ("POST", "/api/cet/attempts", {}),                     # 业务校验失败
        ("POST", "/api/cet/attempts", None),                   # 请求体类型错误
        ("GET", "/api/cet/materials/does-not-exist", None),    # 资源不存在
        ("PATCH", "/api/cet/marks/does-not-exist", {"confirmedSentenceId": "s01"}),
    ]
    for method, path, body in cases:
        kwargs = {"json": body} if body is not None else {}
        if body is None and method in ("POST", "PATCH"):
            kwargs = {"data": "null", "headers": {"Content-Type": "application/json"}}
        r = requests.request(method, f"{BASE}{path}", timeout=TIMEOUT, **kwargs)
        if r.status_code < 400:
            continue  # 该环境行为不同，不在此断言
        try:
            payload = r.json()
        except ValueError:
            pytest.fail(f"{method} {path} 错误响应不是 JSON：{r.text[:120]}")

        detail = payload.get("detail")
        info = payload.get("error_info")
        err = payload.get("error")
        has_something = bool(detail) or bool(info) or bool(err)
        assert has_something, (
            f"{method} {path} 返回 {r.status_code} 但响应体没有 "
            f"detail/error_info/error 任一字段，页面无法显示错误：{payload}"
        )
        # error_info 若存在，必须带可判定的 code（前端据此分支）
        if info:
            assert info.get("code"), f"{path} 的 error_info 缺少 code，前端无法分支"


@pytest.mark.contract
def test_malformed_json_does_not_crash():
    """畸形请求体不能 5xx —— 5xx 意味着页面只能显示「服务器错误」。"""
    bad_bodies = ["", "null", "[]", "garbage", '{"unclosed": ']
    endpoints = [
        ("POST", "/api/cet/attempts"),
        ("POST", "/api/cet/marks"),
        ("POST", "/api/cet/practices"),
        ("POST", "/api/cet/reviews"),
    ]
    for method, path in endpoints:
        for body in bad_bodies:
            r = requests.request(
                method, f"{BASE}{path}", data=body,
                headers={"Content-Type": "application/json"}, timeout=TIMEOUT,
            )
            assert r.status_code < 500, (
                f"{method} {path} 对请求体 {body!r} 返回 {r.status_code}，"
                f"应返回 4xx 让页面能提示：{r.text[:160]}"
            )


@pytest.mark.contract
def test_not_found_does_not_leak_internal_paths():
    """错误信息不能泄露服务器绝对路径 —— 交付物与演示截图会暴露机器信息。"""
    r = requests.get(f"{BASE}/api/cet/materials/../../../etc/passwd", timeout=TIMEOUT)
    text = r.text
    for leak in ("/Users/", "/home/", "C:\\", "/private/"):
        assert leak not in text, f"错误响应泄露了本机路径 {leak}：{text[:200]}"


# ────────────────────────── flow：十分钟复习的时间预算 ──────────────────────────
# 任务书 7.1 要求「预算和用时用毫秒」，10 分钟 = 600000ms。
# C 实现了 budgetMs 与 budgetExhausted，但此前**没有任何测试验证预算耗尽后的行为**——
# 这是真缺口：预算逻辑若写错，用户会遇到「时间到了但还能一直练」或「卡死」。
# 以下三条锁定时长上限、耗尽后可正常结束、耗尽后不再计时。


@pytest.mark.flow
def test_review_budget_caps_effective_time():
    """单次 tick 超过剩余预算时，有效用时必须被截断在预算内，不能超。"""
    reset_mock()
    m = _make_mark_at(36.9)
    _finish_practice(m, "sound")
    review = api("POST", "/api/cet/reviews",
                 json={"maxItems": 3, "budgetMs": 10_000}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})

    r = api("POST", f"/api/cet/reviews/{rid}/actions",
            json={"action": "tick", "elapsedMs": 15_000, "visible": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effectiveMs"] == 10_000, (
        f"预算 10000ms，tick 15000ms 后有效用时应截断为 10000，实际 {body['effectiveMs']}"
    )
    assert body["countedMs"] == 10_000, "计入量应等于被截断后的量"
    assert body["budgetExhausted"] is True, "用尽预算后应标记 budgetExhausted"


@pytest.mark.flow
def test_review_can_finish_after_budget_exhausted():
    """预算耗尽不能把会话卡死 —— 仍必须能标记条目完成并结束。"""
    reset_mock()
    m = _make_mark_at(36.9)
    _finish_practice(m, "sound")
    review = api("POST", "/api/cet/reviews",
                 json={"maxItems": 3, "budgetMs": 5_000}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    api("POST", f"/api/cet/reviews/{rid}/actions",
        json={"action": "tick", "elapsedMs": 6_000, "visible": True})

    item_id = review["items"][0]["itemId"]
    done = api("POST", f"/api/cet/reviews/{rid}/actions",
               json={"action": "done", "itemId": item_id})
    assert done.status_code == 200, (
        f"预算耗尽后仍应能结束条目，实际 {done.status_code}：{done.text[:200]}"
    )
    body = done.json()
    assert body["status"] in CONTRACT_ENUMS["reviewStatus"], body["status"]
    target = next(i for i in body["items"] if i["itemId"] == item_id)
    assert target["status"] == "done"
    assert body["effectiveMs"] == 5_000, "结束后有效用时不应变化"


@pytest.mark.flow
def test_review_no_more_time_counted_after_budget_exhausted():
    """预算用尽后继续 tick，不应再累加有效用时（否则「十分钟」形同虚设）。"""
    reset_mock()
    m = _make_mark_at(36.9)
    _finish_practice(m, "sound")
    review = api("POST", "/api/cet/reviews",
                 json={"maxItems": 3, "budgetMs": 5_000}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    api("POST", f"/api/cet/reviews/{rid}/actions",
        json={"action": "tick", "elapsedMs": 6_000, "visible": True})

    again = api("POST", f"/api/cet/reviews/{rid}/actions",
                json={"action": "tick", "elapsedMs": 3_000, "visible": True})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["effectiveMs"] == 5_000, (
        f"预算已用尽，再 tick 不应累加，实际 {body['effectiveMs']}"
    )
    assert body["countedMs"] == 0, "预算外的时间不应计入"


@pytest.mark.flow
def test_review_budget_rejects_non_positive():
    """预算是毫秒正整数；0 或负数必须被拒，否则「十分钟」无从约束。"""
    reset_mock()
    for bad in (0, -1000):
        r = api("POST", "/api/cet/reviews", json={"maxItems": 3, "budgetMs": bad})
        assert r.status_code in (400, 422), (
            f"budgetMs={bad} 应被拒绝，实际 {r.status_code}：{r.text[:150]}"
        )


@pytest.mark.flow
def test_review_caps_items_at_three():
    """任务书要求「最多三处」。有 4 处卡点时，必须只取 3 处。

    C 之前只把 maxItems=3 当参数传，从未验证上限真的生效 ——
    若实现忽略上限，用户会遇到「说好十分钟复习三处，结果列了十处」。
    """
    reset_mock()
    # 造 4 处已完成的卡点
    for pressed in (36.9, 60.0, 86.0, 98.5):
        m = _make_mark_at(pressed)
        _finish_practice(m, "sound")

    pending = api("GET", "/api/cet/review-items",
                  params={"materialId": MATERIAL_ID}).json()["items"]
    assert len(pending) >= 4, f"应至少有 4 处待复习，实际 {len(pending)}"

    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    assert len(review["items"]) == 3, (
        f"最多的三处：4 处卡点应只取 3 条，实际 {len(review['items'])}"
    )
    # order 应为 1,2,3 且不重复
    orders = [i["order"] for i in review["items"]]
    assert orders == [1, 2, 3], f"条目顺序应为 1,2,3，实际 {orders}"
    # 每条都要绑定真实练习 ID
    for item in review["items"]:
        assert item.get("practiceId"), f"条目 {item['itemId']} 未绑定练习 ID"


@pytest.mark.flow
def test_review_items_are_deterministically_ordered():
    """同样的数据重复创建复习，条目顺序必须一致（不能随机挑三处）。

    否则用户每次进复习看到的都是不同的一组，无法形成复习节奏。
    """
    reset_mock()
    for pressed in (36.9, 60.0, 86.0, 98.5):
        m = _make_mark_at(pressed)
        _finish_practice(m, "sound")

    first = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    second = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    first_ids = [i["practiceId"] for i in first["items"]]
    second_ids = [i["practiceId"] for i in second["items"]]
    assert first_ids == second_ids, (
        f"同样数据两次创建复习，条目应一致；实际 {first_ids} vs {second_ids}"
    )


@pytest.mark.flow
def test_mark_snaps_to_correct_sentence_for_all_sentences(sentences):
    """按键落在某句中间时，必须吸附到**那一句**，不能吸偏。

    C 之前只断言「有 suggestedSentenceId」，从没验证它吸得对。
    这是 T02/T04 的核心：吸偏了用户回听的就是错句子。
    逐句验证全部 32 句，不抽样。
    """
    reset_mock()
    wrong = []
    for s in sentences:
        mid = round((s["startSec"] + s["endSec"]) / 2, 3)
        r = api("POST", "/api/cet/marks", json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique(f"snap-{s['sentenceId']}"),
            "pressedAtSec": mid,
        })
        assert r.status_code in (200, 201), r.text
        got = r.json().get("suggestedSentenceId")
        if got != s["sentenceId"]:
            wrong.append((s["sentenceId"], mid, got))
    assert not wrong, (
        f"{len(wrong)} 处吸附错误（期望句, 按键秒, 实际句）：{wrong[:6]}"
    )


@pytest.mark.flow
def test_mark_snaps_to_boundary_belonging_sentence(sentences):
    """按键正好落在句子起点/终点时，应归到该句（边界不能漏或重）。"""
    reset_mock()
    probe = sentences[2]
    for label, t in (("起点", probe["startSec"]), ("终点", probe["endSec"])):
        r = api("POST", "/api/cet/marks", json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique(f"edge-{label}"),
            "pressedAtSec": t,
        })
        assert r.status_code in (200, 201), r.text
        got = r.json().get("suggestedSentenceId")
        assert got == probe["sentenceId"], (
            f"{probe['sentenceId']} 的{label} {t}s 应吸到本句，实际吸到 {got}"
        )


@pytest.mark.flow
def test_playback_under_threshold_not_counted():
    """任务书 7.2 要求「播完门槛」：没播完的播放不能计入有效学习时长。

    C 此前把所有 playedMs 直接累加 —— 用户点一下跳过也被算成学习时间，
    时长统计就失去意义。这条锁定「未达门槛不计时」。
    """
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    # 只播 500ms，远低于句子时长（~4.6s）的 80%
    r = api("POST", f"/api/cet/practices/{pid}/events", json={
        "eventId": _unique("thr-low"), "type": "end", "playedMs": 500, "visible": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["countedMs"] == 0, (
        f"只播 500ms（未达播完门槛）不应计时，实际计了 {body['countedMs']}ms"
    )
    assert body["effectiveMs"] == 0, f"有效用时应仍为 0，实际 {body['effectiveMs']}"


@pytest.mark.flow
def test_playback_above_threshold_is_counted():
    """播到足够比例后必须正常计时，否则用户真的练了却不被记录。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    r = api("POST", f"/api/cet/practices/{pid}/events", json={
        "eventId": _unique("thr-high"), "type": "end", "playedMs": 4000, "visible": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["countedMs"] == 4000, (
        f"播 4000ms（超过门槛）应计时，实际 {body['countedMs']}ms"
    )
    assert body["effectiveMs"] == 4000


@pytest.mark.flow
def test_playback_threshold_reports_expected_duration():
    """门槛判定要可核对：响应里应给出期望时长与实际比例。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    body = api("POST", f"/api/cet/practices/{pid}/events", json={
        "eventId": _unique("thr-info"), "type": "end", "playedMs": 1000, "visible": True,
    }).json()

    pt = body.get("playThrough")
    assert pt, "响应应带 playThrough 说明门槛判定过程（便于前端与测试核对）"
    assert pt.get("expectedMs"), "应给出该步骤音频期望时长"
    assert 0 < pt.get("ratio", 0) <= 1, f"门槛比例应在 (0,1]，实际 {pt.get('ratio')}"
    assert pt.get("passed") is False, "播 1000ms 不应通过门槛"


@pytest.mark.flow
def test_marks_survive_relist_stable_ids():
    """任务书 7.2 要求验证标记的「刷新恢复」：重新查询列表时，
    标记必须还在、ID 不变、原始按键秒数与确认句子都不丢。

    这条模拟用户刷新回听页：页面重新 GET /marks，数据要能完整还原，
    否则用户刚标的卡点一刷新就没了。
    """
    reset_mock()
    session = _unique("listen-refresh")
    created = api("POST", "/api/cet/marks", json={
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "listeningSessionId": session,
        "pressedAtSec": 36.9,
    })
    assert created.status_code in (200, 201), created.text
    mark = created.json()
    mid = mark["markId"]

    # 用户确认句子
    api("PATCH", f"/api/cet/marks/{mid}",
        json={"confirmedSentenceId": mark["suggestedSentenceId"]})

    # 「刷新」= 重新查询列表
    first = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID})
    assert first.status_code == 200, first.text
    again = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID})
    assert again.status_code == 200, again.text

    def locate(payload):
        return next((m for m in payload["items"] if m["markId"] == mid), None)

    a, b = locate(first.json()), locate(again.json())
    assert a is not None, "刷新后标记消失了"
    assert b is not None, "第二次刷新后标记消失"
    assert a["markId"] == b["markId"] == mid, "标记 ID 在多次查询间必须稳定"
    assert a["pressedAtSec"] == pytest.approx(36.9, abs=0.01), "原始按键秒数不能丢"
    assert a["confirmedSentenceId"] == mark["suggestedSentenceId"], "确认句子不能丢"
    assert a["confirmedAtSec"], "确认时间应保留，供事后追查"


@pytest.mark.flow
def test_marks_from_multiple_sessions_are_all_listed():
    """同一材料下的多次听音会话，标记都要能查回（不能只留最后一次）。"""
    reset_mock()
    mids = []
    for i in range(3):
        r = api("POST", "/api/cet/marks", json={
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "listeningSessionId": _unique(f"listen-multi-{i}"),
            "pressedAtSec": 20.0 + i * 15,
        })
        assert r.status_code in (200, 201), r.text
        mids.append(r.json()["markId"])

    listing = api("GET", "/api/cet/marks", params={"materialId": MATERIAL_ID}).json()
    listed_ids = {m["markId"] for m in listing["items"]}
    missing = [m for m in mids if m not in listed_ids]
    assert not missing, f"这些标记没被列出：{missing}"


# ────────────────────────── flow：模型请求状态与取消 ──────────────────────────
# 任务书 7.2 末句明确：「异步模型操作需要可查询状态或可取消的实现」。
# C 原先只有 idle/complete 两态，既无法告诉用户「正在处理」，也无法取消。
# 以下覆盖 pending 可查、可取消、取消后迟到结果被拒、失败/输出无效两种异常。


@pytest.mark.flow
def test_ai_request_starts_pending_and_is_queryable():
    """发起后应能被查询到 pending —— 前端据此显示「正在处理」。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    def fire():
        api("POST", f"/api/cet/practices/{pid}/next",
            json={"feedback": "meaning", "aiMode": "slow", "aiPendingSec": 1.2})

    t = threading.Thread(target=fire, daemon=True)
    t.start()
    time.sleep(0.5)

    st = api("GET", f"/api/cet/practices/{pid}/ai-status")
    assert st.status_code == 200, st.text
    body = st.json()
    status = body["aiRequest"]["status"]
    assert status in CONTRACT_ENUMS["aiRequestStatus"], f"状态 {status!r} 不合契约枚举"
    assert status == "pending", f"请求进行中应为 pending，实际 {status}"
    assert body["aiRequest"].get("requestId"), "pending 时应能拿到 requestId 以便取消"
    t.join(timeout=10)


@pytest.mark.flow
def test_ai_request_can_be_cancelled():
    """pending 时必须可取消；取消后状态转 cancelled 且带原因。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    t = threading.Thread(target=lambda: api(
        "POST", f"/api/cet/practices/{pid}/next",
        json={"feedback": "meaning", "aiMode": "slow", "aiPendingSec": 1.5}), daemon=True)
    t.start()
    time.sleep(0.5)

    r = api("POST", f"/api/cet/practices/{pid}/ai-cancel", json={})
    assert r.status_code == 200, f"pending 应可取消：{r.text[:200]}"
    ai = r.json()["aiRequest"]
    assert ai["status"] == "cancelled", f"取消后应为 cancelled，实际 {ai['status']}"
    assert ai.get("errorCode") == "USER_CANCELLED", "取消应带原因码，便于前端区分"

    # 重复取消幂等，不报错
    again = api("POST", f"/api/cet/practices/{pid}/ai-cancel", json={})
    assert again.status_code == 200, f"重复取消应幂等：{again.text[:150]}"
    t.join(timeout=10)


@pytest.mark.flow
def test_cancelled_request_late_result_is_rejected():
    """取消后迟到的模型结果不得覆盖当前状态（任务书 T09）。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    holder: dict = {}

    def fire():
        holder["resp"] = api(
            "POST", f"/api/cet/practices/{pid}/next",
            json={"feedback": "meaning", "aiMode": "slow", "aiPendingSec": 1.5})

    t = threading.Thread(target=fire, daemon=True)
    t.start()
    time.sleep(0.4)
    api("POST", f"/api/cet/practices/{pid}/ai-cancel", json={})
    t.join(timeout=15)

    resp = holder.get("resp")
    assert resp is not None, "请求线程未返回"
    assert resp.status_code in (409, 422), (
        f"取消后迟到的结果应被拒绝，实际 {resp.status_code}：{resp.text[:200]}"
    )
    # 状态必须仍是 cancelled，不能被覆盖成 complete
    st = api("GET", f"/api/cet/practices/{pid}/ai-status").json()
    assert st["aiRequest"]["status"] == "cancelled", (
        f"迟到结果不得覆盖状态，实际 {st['aiRequest']['status']}"
    )


@pytest.mark.flow
def test_ai_failure_is_reported_with_code():
    """模型超时必须返回可判定错误码，且状态转 failed（供前端显示重试）。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", "/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]

    r = api("POST", f"/api/cet/practices/{pid}/next",
            json={"feedback": "sound", "aiMode": "fail"})
    assert r.status_code >= 400, f"模拟失败应返回错误，实际 {r.status_code}"
    body = r.json()
    info = body.get("error_info") or {}
    assert info.get("code"), f"错误应带 code 供前端分支：{body}"

    st = api("GET", f"/api/cet/practices/{pid}/ai-status").json()
    assert st["aiRequest"]["status"] == "failed", (
        f"失败后状态应为 failed，实际 {st['aiRequest']['status']}"
    )


@pytest.mark.flow
def test_ai_invalid_output_is_rejected_not_written():
    """模型输出无效时必须拒绝，不能把无效内容写进步骤（任务书 D2 要求）。"""
    reset_mock()
    mark = _create_confirmed_mark()
    pid = api("POST", f"/api/cet/practices", json={"markId": mark["markId"]}).json()["practiceId"]
    before = api("GET", f"/api/cet/practices/{pid}").json()

    r = api("POST", f"/api/cet/practices/{pid}/next",
            json={"feedback": "meaning", "aiMode": "invalid"})
    assert r.status_code >= 400, f"输出无效应报错，实际 {r.status_code}"

    after = api("GET", f"/api/cet/practices/{pid}").json()
    assert after["currentStepId"] == before["currentStepId"], (
        "输出无效时不得推进步骤，否则用户会看到半成品内容"
    )
    assert after["stepCount"] == before["stepCount"], "无效输出不得新增步骤"


@pytest.mark.flow
def test_review_replace_swaps_in_an_unlisted_item():
    """任务书 7.2 明确要求「替换」：把当前条目换成一个尚未纳入的卡点。

    典型场景：用户看到前三处，觉得某处已经会了，想换成另一处没练过的。
    缺少这个能力，用户只能接受系统安排的三处。
    """
    reset_mock()
    for pressed in (36.9, 60.0, 86.0, 98.5):
        m = _make_mark_at(pressed)
        _finish_practice(m, "sound")

    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    before = [i["practiceId"] for i in review["items"]]
    target_item = review["items"][0]["itemId"]

    r = api("POST", f"/api/cet/reviews/{rid}/actions",
            json={"action": "replace", "itemId": target_item})
    assert r.status_code == 200, (
        f"「替换」应被支持（任务书 7.2），实际 {r.status_code}：{r.text[:200]}"
    )
    body = r.json()
    after = [i["practiceId"] for i in body["items"]]

    assert len(after) == 3, f"替换后条目数应仍为 3，实际 {len(after)}"
    assert after[0] != before[0], "被替换的条目应换成新的练习"
    assert after[0] not in before, "换入的必须是原本不在复习里的卡点"
    # 其余条目不应被动到
    assert after[1:] == before[1:], "替换只应影响目标条目，其他条目不能被改"


@pytest.mark.flow
def test_review_replace_keeps_provenance():
    """替换后要能追溯原来那一处是什么（replacedFrom），否则用户无从回顾。"""
    reset_mock()
    for pressed in (36.9, 60.0, 86.0, 98.5):
        m = _make_mark_at(pressed)
        _finish_practice(m, "sound")
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})
    original = review["items"][0]

    body = api("POST", f"/api/cet/reviews/{rid}/actions",
               json={"action": "replace", "itemId": original["itemId"]}).json()
    row = body["items"][0]
    prov = row.get("replacedFrom")
    assert prov, "替换后应记录 replacedFrom，保留被换下的那一处"
    assert prov.get("practiceId") == original["practiceId"], (
        f"replacedFrom 应指向原练习 {original['practiceId']}，实际 {prov}"
    )


@pytest.mark.flow
def test_review_replace_without_candidates_is_rejected():
    """没有可替换的卡点时必须明确报错，不能静默什么都不做。"""
    reset_mock()
    m = _make_mark_at(36.9)
    _finish_practice(m, "sound")   # 只有一处卡点
    review = api("POST", "/api/cet/reviews", json={"maxItems": 3}).json()
    rid = review["reviewId"]
    api("POST", f"/api/cet/reviews/{rid}/actions", json={"action": "start"})

    r = api("POST", f"/api/cet/reviews/{rid}/actions",
            json={"action": "replace", "itemId": review["items"][0]["itemId"]})
    assert r.status_code in (400, 409, 422), (
        f"没有其他卡点可换时应明确报错，实际 {r.status_code}：{r.text[:150]}"
    )


def test_report_target_environment():
    """把当前测试目标写进输出，避免把 Mock 结果误记为真实验收。"""
    info = requests.get(f"{BASE}/api/cet/_mock/info", timeout=3) if IS_MOCK else None
    if info is not None and info.status_code == 200:
        print(f"\n[目标环境] Mock API {BASE}（契约通过，不代表真实业务正确）")
    else:
        print(f"\n[目标环境] 真实/未知服务 {BASE}（结果计为真实服务契约检查）")
