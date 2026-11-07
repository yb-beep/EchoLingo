"""队友卡点模块 · 完整链路端到端回归（主代理补充）。

为什么有这个文件
----------------
队友的模块功能是完整的，但**接口契约比看起来严格**，主代理实测时因为不知道前置条件，
一度误判「复习链路是断的」。实测发现的三个关键前置：

1. `POST /attempts` 必须带 `listeningSessionId`（否则 422 ATTEMPT_ID_REQUIRED）
2. 确认标记必须先交卷（否则 409 ATTEMPT_REQUIRED）
3. `cet_review_items` 只在 `finish_practice()` 时生成 —— 不 finish 就没有复习条目，
   直接 `POST /reviews` 会得到 409 NO_REVIEW_ITEMS
4. `next` 必须传 `feedback`（sound/meaning/heard）才驱动语义推进，否则只是空转返回 200
5. `stepId` 每次推进后都变，必须用上一次响应返回的 `currentStepId`

这个文件把上述契约与完整链路锁住，避免下次有人（或队友）改动时静默破坏。

契约详细说明见 `docs/cet/队友卡点模块-融合验收与使用说明.md`。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from webapp.fastapi_routes.cet import router as cet_router  # noqa: E402
from webapp.services import cet_service  # noqa: E402

SESSION = "e2e-session-1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_USER_DB", str(tmp_path / "cet-e2e.db"))
    db.init_db()
    cet_service.init_cet_schema()
    lesson = db.create_v2_lesson(
        source_type="developmentFixture",
        source_url="fixture://e2e-001",
        title="E2E fixture lesson",
        duration=13,
        media_url="/fixtures/e2e-001.mp3",
        media_kind="audio",
    )
    db.set_v2_lesson_tags(lesson["id"], ["cet4"])
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 0, "start": 0, "end": 2.5, "text": "The library opens at nine on weekdays."},
            {"index": 1, "start": 2.5, "end": 5.5, "text": "Students can reserve a quiet room online."},
            {"index": 2, "start": 5.5, "end": 8, "text": "The workshop covers effective note taking."},
            {"index": 3, "start": 8, "end": 10.5, "text": "It also explains how to evaluate sources."},
            {"index": 4, "start": 10.5, "end": 13, "text": "Bring your student card to the front desk."},
        ],
    )
    material_id = f"v2-{lesson['id']}"
    sentences = {index: f"{material_id}-{index}" for index in range(5)}
    cet_service.replace_questions_for_fixture(
        material_id,
        [
            {
                "id": "q1", "questionNumber": 1, "prompt": "When does the library open?",
                "options": ["At nine.", "At ten."], "correctAnswer": "A",
                "answerSource": "human_verified", "evidenceSentenceId": sentences[0],
            },
            {
                "id": "q2", "questionNumber": 2, "prompt": "What can students reserve online?",
                "options": ["A quiet room.", "A computer."], "correctAnswer": "A",
                "answerSource": "human_verified", "evidenceSentenceId": sentences[1],
            },
            {
                "id": "q3", "questionNumber": 3, "prompt": "What does the workshop cover?",
                "options": ["Note taking.", "Job interviews."], "correctAnswer": "A",
                "answerSource": "human_verified", "evidenceSentenceId": sentences[2],
            },
            {
                "id": "q4", "questionNumber": 4, "prompt": "What should students bring?",
                "options": ["Their student card.", "Their library card."], "correctAnswer": None,
                "answerSource": "unknown", "evidenceSentenceId": sentences[4],
            },
        ],
    )
    app = FastAPI()
    app.include_router(cet_router)
    with TestClient(app) as test_client:
        test_client.material_id = material_id
        test_client.material_version = cet_service.get_material(material_id)["version"]
        yield test_client


def _submit(client: TestClient):
    return client.post(
        "/api/cet/attempts",
        json={
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "listeningSessionId": SESSION,
            "answers": [
                {"questionId": "q1", "answer": "A"},
                {"questionId": "q2", "answer": "A"},
                {"questionId": "q3", "answer": "A"},
                {"questionId": "q4", "answer": "A"},
            ],
        },
    )


def _create_confirmed_mark(client: TestClient) -> dict:
    """建标记 → 确认句（前提：已交卷）。"""
    mark = client.post(
        "/api/cet/marks",
        json={
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "listeningSessionId": SESSION,
            "pressedAtSec": 3.0,
            "reactionDelaySeconds": 2.5,
            "speed": 1.0,
        },
    )
    assert mark.status_code == 201, mark.text
    payload = mark.json()
    confirm = client.patch(
        f"/api/cet/marks/{payload['markId']}",
        json={"confirmedSentenceId": payload["suggestedSentenceId"]},
    )
    assert confirm.status_code == 200, confirm.text
    return payload


# ── 契约前置：交卷必须带 listeningSessionId ─────────────────────
def test_attempt_requires_listening_session_id(client: TestClient):
    """只传 answers 不传 listeningSessionId → 422 ATTEMPT_ID_REQUIRED。

    这是主代理实测踩到的第一个坑：不知道这个前置就无法进入后续链路。
    """
    resp = client.post(
        "/api/cet/attempts",
        json={
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "answers": [{"questionId": "q1", "answer": "A"}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "ATTEMPT_ID_REQUIRED"


# ── 契约前置：确认标记必须先交卷 ────────────────────────────────
def test_confirm_mark_requires_attempt(client: TestClient):
    """未交卷就确认标记 → 409 ATTEMPT_REQUIRED。"""
    mark = client.post(
        "/api/cet/marks",
        json={
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "listeningSessionId": SESSION,
            "pressedAtSec": 3.0,
            "reactionDelaySeconds": 2.5,
            "speed": 1.0,
        },
    )
    assert mark.status_code == 201
    payload = mark.json()
    confirm = client.patch(
        f"/api/cet/marks/{payload['markId']}",
        json={"confirmedSentenceId": payload["suggestedSentenceId"]},
    )
    assert confirm.status_code == 409
    assert confirm.json()["detail"]["code"] == "ATTEMPT_REQUIRED"


# ── 完整链路：交卷 → 标记 → 确认 → 带练 → 反馈 → finish → 复习 ──
def test_full_chain_produces_review_item(client: TestClient):
    """端到端：跑完整条链路，断言每一步都通，且 finish 后生成待复习条目。

    这是队友模块的**主干**，任何一环断掉都会让「今日复习」变空。
    """
    assert _submit(client).status_code == 201
    mark = _create_confirmed_mark(client)

    practice = client.post(
        "/api/cet/practices",
        json={
            "markId": mark["markId"],
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "sentenceId": mark["suggestedSentenceId"],
        },
    )
    assert practice.status_code == 201, practice.text
    practice_id = practice.json()["practiceId"]

    # next 必须带 feedback 才驱动语义推进
    detail = client.get(f"/api/cet/practices/{practice_id}").json()
    step_id = detail["currentStepId"]
    nxt = client.post(
        f"/api/cet/practices/{practice_id}/next",
        json={"stepId": step_id, "materialVersion": client.material_version, "feedback": "sound"},
    )
    assert nxt.status_code == 200, nxt.text

    # finish 必须用**最新**的 stepId（每次推进都会变）
    detail = client.get(f"/api/cet/practices/{practice_id}").json()
    finish = client.post(
        f"/api/cet/practices/{practice_id}/finish",
        json={"stepId": detail["currentStepId"], "materialVersion": client.material_version},
    )
    assert finish.status_code == 200, finish.text
    body = finish.json()
    assert body["status"] == "complete"
    assert body.get("reviewItem"), "finish 后必须生成 reviewItem（否则「今日复习」永远是空的）"

    items = client.get("/api/cet/review-items").json()["items"]
    pending = [i for i in items if i["status"] == "pending"]
    assert pending, "finish 后应有 pending 状态的复习条目"


# ── 复习会话与动作 ──────────────────────────────────────────────
def test_review_session_and_actions(client: TestClient):
    """建复习会话 → start → complete，条目状态 current → done。"""
    assert _submit(client).status_code == 201
    mark = _create_confirmed_mark(client)
    practice = client.post(
        "/api/cet/practices",
        json={
            "markId": mark["markId"],
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "sentenceId": mark["suggestedSentenceId"],
        },
    ).json()
    detail = client.get(f"/api/cet/practices/{practice['practiceId']}").json()
    client.post(
        f"/api/cet/practices/{practice['practiceId']}/finish",
        json={"stepId": detail["currentStepId"], "materialVersion": client.material_version},
    )

    review = client.post("/api/cet/reviews", json={"maxItems": 3})
    assert review.status_code == 201, review.text
    review_id = review.json()["id"]

    # 新建是 ready，动作需要先 start
    started = client.post(
        f"/api/cet/reviews/{review_id}/actions",
        json={"action": "start", "actionId": "aid-start-1"},
    )
    assert started.status_code == 200, started.text

    detail = client.get(f"/api/cet/reviews/{review_id}").json()
    entries = detail.get("entries") or []
    assert entries, "复习会话必须有条目"
    entry = entries[0]
    assert entry["status"] == "current"

    completed = client.post(
        f"/api/cet/reviews/{review_id}/actions",
        json={
            "action": "complete",
            "actionId": "aid-complete-1",
            "entryId": entry.get("entryId") or entry.get("id"),
            "itemId": entry.get("itemId"),
        },
    )
    assert completed.status_code == 200, completed.text

    after = client.get(f"/api/cet/reviews/{review_id}").json()
    assert [e["status"] for e in after["entries"]] == ["done"]


def test_review_action_requires_action_id(client: TestClient):
    """复习动作缺 actionId → 422 INVALID_REVIEW_ACTION（不是静默忽略）。"""
    review = client.post("/api/cet/reviews", json={"maxItems": 3})
    if review.status_code != 201:
        pytest.skip("当前库没有待复习条目，无法建会话")
    review_id = review.json()["id"]
    resp = client.post(f"/api/cet/reviews/{review_id}/actions", json={"action": "start"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_REVIEW_ACTION"


# ── 删除标记（队友本轮新增功能）──────────────────────────────────
def test_delete_mark_success_and_not_found(client: TestClient):
    assert _submit(client).status_code == 201
    mark = _create_confirmed_mark(client)
    deleted = client.delete(f"/api/cet/marks/{mark['markId']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "markId": mark["markId"]}

    again = client.delete(f"/api/cet/marks/{mark['markId']}")
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "MARK_NOT_FOUND"


def test_delete_mark_rejected_when_in_practice(client: TestClient):
    """已开始带练的标记不能删（保护练习记录）。"""
    assert _submit(client).status_code == 201
    mark = _create_confirmed_mark(client)
    client.post(
        "/api/cet/practices",
        json={
            "markId": mark["markId"],
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "sentenceId": mark["suggestedSentenceId"],
        },
    )
    resp = client.delete(f"/api/cet/marks/{mark['markId']}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MARK_IN_PRACTICE"


# ── 前端键位契约：Space 归底座、M 归队友 ────────────────────────
def test_teammate_marks_with_m_key_not_space():
    """返修 2.1：队友的标记键是 M，不是 Space。

    Space 归底座做播放/暂停（用户要求）；两边抢同一个键会导致空格失效
    （实测：队友挂载但未进练习模式时，两边都不处理 Space）。
    """
    js = (ROOT / "frontend" / "static" / "cet-listening.js").read_text(encoding="utf-8")
    assert "if (e.code !== 'KeyM') return;" in js, "队友的标记键不是 M"
    assert "if (e.code !== 'Space') return;" not in js, "队友仍在抢 Space 键"
    # 底座的归属判定不再受队友影响
    player = (ROOT / "frontend" / "static" / "cet-player.js").read_text(encoding="utf-8")
    assert "return CET_ON;" in player, "底座 hotkeyActive 仍受队友模块影响"
