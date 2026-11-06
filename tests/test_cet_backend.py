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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_USER_DB", str(tmp_path / "cet-test.db"))
    db.init_db()
    cet_service.init_cet_schema()
    lesson = db.create_v2_lesson(
        source_type="developmentFixture",
        source_url="fixture://cet-001",
        title="Campus library workshop",
        duration=13,
        media_url="/fixtures/cet-001.mp3",
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
    candidate = db.create_v2_lesson(
        source_type="developmentFixture",
        source_url="fixture://transfer-001",
        title="Unseen library announcement",
        duration=3,
        media_url="/fixtures/transfer-001.mp3",
        media_kind="audio",
    )
    db.replace_v2_subtitle_segments(
        candidate["id"],
        [{"index": 0, "start": 0, "end": 3, "text": "Students may reserve quiet study rooms through the library website."}],
    )
    app = FastAPI()
    app.include_router(cet_router)
    with TestClient(app) as test_client:
        test_client.material_id = material_id
        test_client.material_version = cet_service.get_material(material_id)["version"]
        yield test_client


def submit_attempt(client: TestClient, attempt_id: str = "attempt-1"):
    return client.post(
        "/api/cet/attempts",
        json={
            "attemptId": attempt_id,
            "materialId": client.material_id,
            "materialVersion": client.material_version,
            "listeningSessionId": "listen-1",
            "answers": [
                {"questionId": "q1", "answer": "A"},
                {"questionId": "q2", "answer": "B"},
                {"questionId": "q3", "answer": "A"},
                {"questionId": "q4", "answer": "A"},
            ],
        },
    )


def test_material_questions_attempt_and_idempotency(client: TestClient):
    response = client.get("/api/cet/materials?level=cet4")
    assert response.status_code == 200
    item = response.json()["materials"][0]
    assert item["id"] == client.material_id
    assert item["version"] == client.material_version
    assert item["audioUrl"] == "/fixtures/cet-001.mp3"
    assert item["sentenceCount"] == 5

    detail = client.get(f"/api/cet/materials/{client.material_id}").json()
    # 契约要求顶层为 {material, sentences}（S5 后调整），
    # 句子在外层、材料元信息在 material 内。
    assert detail["sentences"][1]["startSeconds"] == 2.5
    assert detail["material"]["source"]["materialStore"] == "echolingo.v2_lessons"

    questions_response = client.get(f"/api/cet/materials/{client.material_id}/questions")
    assert questions_response.status_code == 200
    serialized = questions_response.text
    assert "correctAnswer" not in serialized
    assert "answerSource" not in serialized
    assert "evidence" not in serialized
    assert len(questions_response.json()["questions"]) == 4

    first = submit_attempt(client)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["score"] == 2
    assert result["gradedCount"] == 3
    assert result["unknownCount"] == 1
    assert result["answers"][1]["status"] == "incorrect"
    assert result["answers"][3]["status"] == "unknown"
    assert result["answers"][3]["correctAnswer"] is None
    assert result["answers"][0]["evidence"]["sentenceId"].endswith("-0")

    replay = client.post(
        "/api/cet/attempts",
        json={
            "attemptId": "attempt-1", "materialId": client.material_id,
            "materialVersion": client.material_version, "answers": [],
        },
    )
    assert replay.status_code == 201
    assert replay.json() == result


def test_mark_confirm_restore_and_validation(client: TestClient):
    assert submit_attempt(client).status_code == 201
    body = {
        "markId": "mark-1", "listeningSessionId": "listen-1",
        "materialId": client.material_id, "materialVersion": client.material_version,
        "rawSeconds": 3.2,
    }
    created = client.post("/api/cet/marks", json=body)
    assert created.status_code == 201, created.text
    mark = created.json()
    assert mark["suggestedSentenceId"].endswith("-1")
    assert mark["confirmedSentenceId"] is None

    assert client.post("/api/cet/marks", json=body).json() == mark
    listed = client.get(f"/api/cet/marks?materialId={client.material_id}").json()["marks"]
    assert listed == [mark]

    confirmed = client.patch(
        "/api/cet/marks/mark-1",
        json={"confirmedSentenceId": f"{client.material_id}-0", "materialVersion": client.material_version},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmedSentenceId"].endswith("-0")
    assert client.get("/api/cet/marks", params={"materialId": client.material_id}).json()["marks"][0]["confirmedSentenceId"].endswith("-0")

    invalid = client.post("/api/cet/marks", json={**body, "markId": "mark-2", "rawSeconds": 99})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INVALID_AUDIO_TIME"
    stale = client.post("/api/cet/marks", json={**body, "markId": "mark-3", "materialVersion": "old"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MATERIAL_VERSION_CHANGED"


def test_mark_delete(client: TestClient):
    body = {
        "markId": "mark-delete", "listeningSessionId": "listen-1",
        "materialId": client.material_id, "materialVersion": client.material_version,
        "rawSeconds": 3.2,
    }
    assert client.post("/api/cet/marks", json=body).status_code == 201
    deleted = client.delete("/api/cet/marks/mark-delete")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "markId": "mark-delete"}
    assert client.get("/api/cet/marks", params={"materialId": client.material_id}).json()["marks"] == []

    missing = client.delete("/api/cet/marks/mark-delete")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "MARK_NOT_FOUND"


def test_mark_in_practice_cannot_be_deleted(client: TestClient):
    create_practice(client, "delete-busy")
    rejected = client.delete("/api/cet/marks/mark-delete-busy")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "MARK_IN_PRACTICE"


def test_empty_and_missing_resources(client: TestClient):
    # 契约要求顶层为 {items, count}，同时保留 materials 别名兼容旧调用；
    # 原断言写死全等 {"materials": []}，在契约字段存在时必然不等，已放宽为检查空列表。
    _empty = client.get("/api/cet/materials?level=cet6").json()
    assert _empty.get("items") == [] and _empty.get("count") == 0
    missing = client.get("/api/cet/materials/v2-99999")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "MATERIAL_NOT_FOUND"
    assert client.get("/api/cet/marks?materialId=v2-99999").status_code == 404


def create_practice(client: TestClient, suffix: str = "1", sentence_index: int = 1):
    if suffix != "existing":
        assert submit_attempt(client, f"attempt-{suffix}").status_code == 201
    raw_seconds = [0.25, 2.75, 5.75, 8.25, 10.75][sentence_index]
    mark = client.post(
        "/api/cet/marks",
        json={
            "markId": f"mark-{suffix}", "listeningSessionId": "listen-1",
            "materialId": client.material_id, "materialVersion": client.material_version,
            "rawSeconds": raw_seconds,
        },
    )
    assert mark.status_code == 201, mark.text
    confirmed = client.patch(
        f"/api/cet/marks/mark-{suffix}",
        json={"confirmedSentenceId": f"{client.material_id}-{sentence_index}", "materialVersion": client.material_version},
    )
    assert confirmed.status_code == 200, confirmed.text
    practice = client.post(
        "/api/cet/practices", json={"practiceId": f"practice-{suffix}", "markId": f"mark-{suffix}"}
    )
    assert practice.status_code == 201, practice.text
    return practice.json()


def practice_body(client: TestClient, practice, **extra):
    return {
        "stepId": practice["currentStepId"], "materialVersion": client.material_version, **extra,
    }


def play_current(client: TestClient, practice, prefix: str, review_id: str | None = None):
    step = practice["currentStep"]
    start = step["playRange"]["startSeconds"]
    end = step["playRange"]["endSeconds"]
    speed = step["speed"]
    active_ms = round((end - start) * 1000 / speed)
    common = {"materialVersion": client.material_version, "stepId": step["id"], "speed": speed}
    if review_id:
        common["reviewId"] = review_id
    started = client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json={**common, "eventId": f"{prefix}-start", "type": "playback_start", "audioTime": start},
    )
    assert started.status_code == 200, started.text
    ended = client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json={**common, "eventId": f"{prefix}-end", "type": "playback_end", "audioTime": end, "activeMs": active_ms},
    )
    assert ended.status_code == 200, ended.text
    return ended.json()


def send_feedback(client: TestClient, practice, feedback: str, event_id: str):
    return client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json=practice_body(client, practice, eventId=event_id, type="feedback", feedback=feedback),
    )


def next_step(client: TestClient, practice, request_id: str, action: str | None = None):
    body = practice_body(client, practice, requestId=request_id)
    if action:
        body["action"] = action
    return client.post(f"/api/cet/practices/{practice['id']}/next", json=body)


def test_practice_feedback_local_help_and_idempotent_events(client: TestClient):
    practice = create_practice(client)
    assert practice["currentStep"]["action"] == "replay"
    assert practice["currentStep"]["showText"] is False
    duplicate = client.post("/api/cet/practices", json={"practiceId": "another", "markId": "mark-1"})
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == practice["id"]

    blocked = send_feedback(client, practice, "sound", "early-feedback")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "PLAYBACK_REQUIRED"

    played = play_current(client, practice, "initial")
    assert played["effectiveMs"] == 3000
    assert played["event"]["playedToEnd"] is True
    replayed_end = client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json=practice_body(
            client, practice, eventId="initial-end", type="playback_end", audioTime=5.5, speed=1, activeMs=3000,
        ),
    )
    assert replayed_end.status_code == 200
    assert replayed_end.json()["effectiveMs"] == 3000
    assert replayed_end.json()["event"]["duplicate"] is True

    feedback = send_feedback(client, practice, "sound", "sound-1")
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["latestFeedback"] == "sound"
    slow = next_step(client, feedback.json(), "next-slow")
    assert slow.status_code == 200, slow.text
    assert slow.json()["currentStep"]["action"] == "slow"
    assert slow.json()["currentStep"]["speed"] == 0.75
    assert slow.json()["currentStep"]["source"] == "local"

    heard = send_feedback(client, play_current(client, slow.json(), "slow"), "heard", "heard-slow")
    hidden = next_step(client, heard.json(), "next-hidden")
    assert hidden.json()["currentStep"]["action"] == "replay"
    assert hidden.json()["currentStep"]["showText"] is False
    restored = client.get(f"/api/cet/practices/{practice['id']}")
    assert restored.json()["currentStepId"] == hidden.json()["currentStepId"]
    assert len(restored.json()["feedbackHistory"]) == 2


def test_ai_explain_transfer_answer_sources_and_finish(client: TestClient, monkeypatch):
    from webapp.services import cet_ai

    def fake_generate(action, sentence, feedback, candidate=None):
        if action == "explain":
            return {"explanation": "这里指网上预约安静房间。", "keyPhrases": ["quiet room"], "source": "ai"}, "fixture-model"
        assert action == "transfer"
        assert candidate["audioUrl"] == "/fixtures/transfer-001.mp3"
        assert candidate["source"]["unseen"] is True
        return {
            "question": "两句都提到了什么服务？", "options": ["预约安静房间", "办理学生证"],
            "correctAnswer": "A", "explanation": "两句都涉及预约学习空间。", "source": "ai",
        }, "fixture-model"

    monkeypatch.setattr(cet_ai, "generate", fake_generate)
    practice = create_practice(client)
    meaning = send_feedback(client, play_current(client, practice, "initial"), "meaning", "meaning-1")
    explained = next_step(client, meaning.json(), "explain-1")
    assert explained.status_code == 200, explained.text
    assert explained.json()["currentStep"]["action"] == "explain"
    assert explained.json()["currentStep"]["source"] == "ai"
    assert explained.json()["currentStep"]["data"]["explanation"] == "这里指网上预约安静房间。"
    assert explained.json()["aiRequest"]["status"] == "succeeded"
    assert explained.json()["aiRequest"]["model"] == "fixture-model"
    assert next_step(client, meaning.json(), "explain-1").json()["currentStepId"] == explained.json()["currentStepId"]

    heard = send_feedback(client, explained.json(), "heard", "heard-explain")
    hidden = next_step(client, heard.json(), "hidden-1").json()
    heard_hidden = send_feedback(client, play_current(client, hidden, "hidden"), "heard", "heard-hidden")
    transfer_response = next_step(client, heard_hidden.json(), "transfer-1")
    assert transfer_response.status_code == 200, transfer_response.text
    transfer = transfer_response.json()
    step = transfer["currentStep"]
    assert step["action"] == "transfer"
    assert step["data"]["candidate"]["materialId"] != client.material_id
    assert step["data"]["candidate"]["audioUrl"] == "/fixtures/transfer-001.mp3"
    assert "correctAnswer" not in step["data"]
    assert "correctAnswer" not in client.get(f"/api/cet/practices/{practice['id']}").text

    answered = play_current(client, transfer, "candidate")
    result = client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json=practice_body(client, answered, eventId="transfer-answer-1", type="transfer_answer", answer="A"),
    )
    assert result.status_code == 200, result.text
    assert result.json()["lastTransferResult"]["isCorrect"] is True
    assert result.json()["lastTransferResult"]["answerSource"] == "ai:fixture-model:transfer-1"
    assert result.json()["lastTransferResult"]["assisted"] is True
    assert result.json()["transferStatus"] == "matched"

    finished = client.post(
        f"/api/cet/practices/{practice['id']}/finish",
        json=practice_body(client, result.json(), finishId="finish-1"),
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["practice"]["status"] == "complete"
    assert finished.json()["reviewItem"]["latestFeedback"] == "heard"
    assert finished.json()["reviewItem"]["transferStatus"] == "matched"
    again = client.post(
        f"/api/cet/practices/{practice['id']}/finish",
        json={"finishId": "finish-again", "stepId": "ignored", "materialVersion": "ignored"},
    )
    assert again.status_code == 200
    assert again.json()["reviewItem"]["id"] == finished.json()["reviewItem"]["id"]
    assert len(client.get("/api/cet/review-items").json()["items"]) == 1


def test_ai_failure_local_fallback_no_candidate_and_latest_feedback(client: TestClient, monkeypatch):
    from webapp.services import cet_ai

    monkeypatch.setattr(
        cet_ai, "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(cet_ai.CETAIError("AI_TIMEOUT", "模型请求超时。", 504)),
    )
    practice = create_practice(client)
    meaning = send_feedback(client, play_current(client, practice, "initial"), "meaning", "meaning-failure")
    failed = next_step(client, meaning.json(), "explain-timeout")
    assert failed.status_code == 504
    assert failed.json()["detail"]["code"] == "AI_TIMEOUT"
    current = client.get(f"/api/cet/practices/{practice['id']}").json()
    assert current["aiRequest"]["status"] == "failed"
    assert current["aiRequest"]["errorCode"] == "AI_TIMEOUT"
    local = client.post(
        f"/api/cet/practices/{practice['id']}/local",
        json=practice_body(client, current, actionId="local-replay", action="replay"),
    )
    assert local.status_code == 200, local.text
    assert local.json()["currentStep"]["source"] == "local"
    heard = send_feedback(client, play_current(client, local.json(), "local-hidden"), "heard", "heard-local")
    assert heard.json()["outcome"] == "heard"
    later_sound = send_feedback(client, heard.json(), "sound", "sound-latest")
    assert later_sound.json()["latestFeedback"] == "sound"
    assert later_sound.json()["outcome"] == "blocked"

    monkeypatch.setattr(cet_service, "_transfer_candidate", lambda conn, row: None)
    # Use a fresh practice whose latest feedback is heard on a hidden full-speed replay.
    other = create_practice(client, "no-candidate", 2)
    heard_other = send_feedback(client, play_current(client, other, "other-initial"), "heard", "other-heard")
    no_candidate = next_step(client, heard_other.json(), "other-transfer")
    assert no_candidate.status_code == 200, no_candidate.text
    assert no_candidate.json()["currentStep"]["action"] == "finish"
    assert no_candidate.json()["currentStep"]["data"]["reason"] == "no_candidate"
    saved = client.post(
        f"/api/cet/practices/{other['id']}/finish",
        json=practice_body(client, no_candidate.json(), finishId="finish-no-candidate"),
    )
    assert saved.status_code == 200
    assert saved.json()["reviewItem"]["transferStatus"] == "no_candidate"


def review_operation(client: TestClient, review_id: str, action: str, action_id: str, entry_id: str | None = None):
    body = {"actionId": action_id, "action": action}
    if entry_id:
        body["currentEntryId"] = entry_id
    return client.post(f"/api/cet/reviews/{review_id}/actions", json=body)


def heartbeat(client: TestClient, practice, review_id: str, event_id: str, active_ms: int = 5000):
    return client.post(
        f"/api/cet/practices/{practice['id']}/events",
        json=practice_body(
            client, practice, eventId=event_id, type="activity_heartbeat", activeMs=active_ms, reviewId=review_id,
        ),
    )


def test_review_three_items_pause_restart_skip_replace_and_budget(client: TestClient):
    saved_items = []
    for index in range(4):
        practice = create_practice(client, f"review-{index}", index)
        finished = client.post(
            f"/api/cet/practices/{practice['id']}/finish",
            json=practice_body(client, practice, finishId=f"finish-review-{index}"),
        )
        assert finished.status_code == 200, finished.text
        saved_items.append(finished.json()["reviewItem"])

    created = client.post("/api/cet/reviews", json={"reviewId": "review-1", "budgetMs": 10000})
    assert created.status_code == 201, created.text
    review = created.json()
    assert review["status"] == "ready"
    assert review["budgetMs"] == 600000 - 590000
    assert len(review["entries"]) == 3
    assert review["currentPractice"]["id"] == review["entries"][0]["practiceId"]
    assert client.post("/api/cet/reviews", json={"reviewId": "review-1", "budgetMs": 10000}).json()["entries"] == review["entries"]

    started = review_operation(client, "review-1", "start", "start-1", review["currentEntryId"])
    assert started.status_code == 200, started.text
    active = started.json()
    original_practice_id = active["currentPractice"]["id"]
    original_step_id = active["currentPractice"]["currentStepId"]
    assert active["currentPractice"]["status"] == "active"
    assert active["currentPractice"]["currentStep"]["source"] == "review"

    paused = review_operation(client, "review-1", "pause", "pause-1", active["currentEntryId"]).json()
    assert paused["status"] == "paused"
    assert paused["pauseCount"] == 1
    paused_heartbeat = heartbeat(client, paused["currentPractice"], "review-1", "paused-heartbeat")
    assert paused_heartbeat.status_code == 200
    assert paused_heartbeat.json()["event"]["appliedMs"] == 0
    assert client.get("/api/cet/reviews/review-1").json()["effectiveMs"] == 0

    resumed = review_operation(client, "review-1", "resume", "resume-1", paused["currentEntryId"]).json()
    assert resumed["status"] == "active"
    assert resumed["currentPractice"]["id"] == original_practice_id
    assert resumed["currentPractice"]["currentStepId"] == original_step_id
    counted = heartbeat(client, resumed["currentPractice"], "review-1", "active-heartbeat")
    assert counted.json()["event"]["appliedMs"] == 5000
    assert client.get("/api/cet/reviews/review-1").json()["effectiveMs"] == 5000
    assert heartbeat(client, resumed["currentPractice"], "review-1", "active-heartbeat").json()["event"]["duplicate"] is True
    assert client.get("/api/cet/reviews/review-1").json()["effectiveMs"] == 5000

    # A process restart recovers an in-flight review as paused without changing its practice or step.
    path = db.current_db_path()
    cet_service._initialized.remove(path)
    cet_service.init_cet_schema()
    recovered = client.get("/api/cet/reviews/review-1").json()
    assert recovered["status"] == "paused"
    assert recovered["pauseCount"] == 2
    assert recovered["currentPractice"]["id"] == original_practice_id
    assert recovered["currentPractice"]["currentStepId"] == original_step_id
    resumed_again = review_operation(client, "review-1", "resume", "resume-2", recovered["currentEntryId"]).json()
    assert resumed_again["currentPractice"]["id"] == original_practice_id

    skipped_item_id = resumed_again["entries"][0]["item"]["id"]
    skipped = review_operation(client, "review-1", "skip", "skip-1", resumed_again["currentEntryId"])
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["entries"][0]["status"] == "skipped"
    assert skipped.json()["currentPractice"]["id"] != original_practice_id
    items_after_skip = {item["id"]: item for item in client.get("/api/cet/review-items").json()["items"]}
    assert items_after_skip[skipped_item_id]["status"] == "pending"

    replaced = review_operation(client, "review-1", "replace", "replace-1", skipped.json()["currentEntryId"])
    assert replaced.status_code == 200, replaced.text
    assert len(replaced.json()["entries"]) == 4
    current_item_id = replaced.json()["entries"][replaced.json()["currentPosition"]]["item"]["id"]
    completed = review_operation(client, "review-1", "complete", "complete-item", replaced.json()["currentEntryId"])
    assert completed.status_code == 200, completed.text
    items_after_complete = {item["id"]: item for item in client.get("/api/cet/review-items").json()["items"]}
    assert items_after_complete[current_item_id]["status"] == "done"

    current = completed.json()
    first_budget = heartbeat(client, current["currentPractice"], "review-1", "budget-heartbeat")
    assert first_budget.status_code == 200
    review_at_budget = client.get("/api/cet/reviews/review-1").json()
    assert review_at_budget["status"] == "complete"
    assert review_at_budget["effectiveMs"] == 10000
    assert review_at_budget["remainingMs"] == 0
    assert review_at_budget["currentPractice"] is None
    extra = heartbeat(client, current["currentPractice"], "review-1", "after-budget")
    assert extra.status_code == 200
    assert extra.json()["event"]["appliedMs"] == 0
    assert client.get("/api/cet/reviews/review-1").json()["effectiveMs"] == 10000


def test_mark_reaction_delay_long_press_and_confirmation_range(client: TestClient):
    assert submit_attempt(client, "attempt-marks").status_code == 201
    body = {
        "markId": "long-1", "listeningSessionId": "listen-1", "materialId": client.material_id,
        "materialVersion": client.material_version, "rawSeconds": 4.0, "speed": 1.0, "reactionDelaySeconds": 0.6,
    }
    first = client.post("/api/cet/marks", json=body)
    assert first.status_code == 201
    assert first.json()["rawSeconds"] == 4.0
    assert first.json()["adjustedSeconds"] == pytest.approx(3.4)
    repeated = client.post("/api/cet/marks", json={**body, "markId": "long-2", "rawSeconds": 4.4})
    assert repeated.status_code == 201
    assert repeated.json()["id"] == "long-1"
    assert len(client.get(f"/api/cet/marks?materialId={client.material_id}").json()["marks"]) == 1
    out_of_range = client.patch(
        "/api/cet/marks/long-1",
        json={"confirmedSentenceId": f"{client.material_id}-4", "materialVersion": client.material_version},
    )
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"]["code"] == "CONFIRMATION_OUT_OF_RANGE"


def test_ai_wait_is_not_timed_and_late_result_is_discarded(client: TestClient, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from webapp.services import cet_ai

    entered = threading.Event()
    release = threading.Event()

    def slow_generate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"explanation": "迟到解释", "keyPhrases": [], "source": "ai"}, "slow-model"

    monkeypatch.setattr(cet_ai, "generate", slow_generate)
    practice = create_practice(client, "stale")
    meaning = send_feedback(client, play_current(client, practice, "stale-initial"), "meaning", "stale-meaning")
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(next_step, client, meaning.json(), "stale-request")
        assert entered.wait(2)
        waiting = client.get(f"/api/cet/practices/{practice['id']}").json()
        timed = client.post(
            f"/api/cet/practices/{practice['id']}/events",
            json=practice_body(client, waiting, eventId="ai-wait-heartbeat", type="activity_heartbeat", activeMs=1000),
        )
        assert timed.status_code == 200
        assert timed.json()["event"]["appliedMs"] == 0
        local = client.post(
            f"/api/cet/practices/{practice['id']}/local",
            json=practice_body(client, timed.json(), actionId="cancel-stale", action="replay"),
        )
        assert local.status_code == 200, local.text
        release.set()
        late = pending.result(timeout=5)
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "STALE_RESPONSE"
    current = client.get(f"/api/cet/practices/{practice['id']}").json()
    assert current["currentStepId"] == local.json()["currentStepId"]
    assert current["aiRequest"]["status"] == "stale"
    assert current["effectiveMs"] == 3000


def test_restart_recovers_pending_ai_without_applying_late_result(client: TestClient, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from webapp.services import cet_ai

    entered = threading.Event()
    release = threading.Event()

    def slow_generate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"explanation": "重启前的迟到解释", "keyPhrases": [], "source": "ai"}, "slow-model"

    monkeypatch.setattr(cet_ai, "generate", slow_generate)
    practice = create_practice(client, "restart-ai")
    meaning = send_feedback(client, play_current(client, practice, "restart-ai-play"), "meaning", "restart-ai-feedback")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(next_step, client, meaning.json(), "restart-ai-request")
        assert entered.wait(2)
        path = db.current_db_path()
        cet_service._initialized.remove(path)
        cet_service.init_cet_schema()
        recovered = client.get(f"/api/cet/practices/{practice['id']}").json()
        assert recovered["aiRequest"]["status"] == "failed"
        assert recovered["aiRequest"]["errorCode"] == "SERVER_RESTARTED"
        release.set()
        late = pending.result(timeout=5)
    assert late.status_code == 409
    current = client.get(f"/api/cet/practices/{practice['id']}").json()
    assert current["currentStepId"] == meaning.json()["currentStepId"]
    assert current["aiRequest"]["status"] == "stale"


def test_concurrent_duplicate_writes_are_idempotent(client: TestClient):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as executor:
        attempts = list(executor.map(lambda _: submit_attempt(client, "attempt-concurrent"), range(6)))
    assert all(response.status_code == 201 for response in attempts)
    assert {response.json()["score"] for response in attempts} == {2}
    body = {
        "markId": "mark-concurrent", "listeningSessionId": "listen-1", "materialId": client.material_id,
        "materialVersion": client.material_version, "rawSeconds": 3.2,
    }
    with ThreadPoolExecutor(max_workers=6) as executor:
        marks = list(executor.map(lambda _: client.post("/api/cet/marks", json=body), range(6)))
    assert all(response.status_code == 201 for response in marks)
    assert {response.json()["id"] for response in marks} == {"mark-concurrent"}
    assert client.patch(
        "/api/cet/marks/mark-concurrent",
        json={"confirmedSentenceId": f"{client.material_id}-1", "materialVersion": client.material_version},
    ).status_code == 200
    practice = client.post(
        "/api/cet/practices", json={"practiceId": "practice-concurrent", "markId": "mark-concurrent"}
    ).json()
    event = practice_body(client, practice, eventId="heartbeat-concurrent", type="activity_heartbeat", activeMs=1000)
    with ThreadPoolExecutor(max_workers=6) as executor:
        events = list(executor.map(lambda _: client.post(f"/api/cet/practices/{practice['id']}/events", json=event), range(6)))
    assert all(response.status_code == 200 for response in events)
    assert client.get(f"/api/cet/practices/{practice['id']}").json()["effectiveMs"] == 1000


def test_model_output_validation_and_missing_key(client: TestClient, monkeypatch):
    from webapp.runtime import ai_config
    from webapp.services import cet_ai

    sentence = {
        "sentenceId": "s1", "text": "Students can reserve a quiet room online.", "startSeconds": 0, "endSeconds": 3,
    }
    real_call = cet_ai._call
    monkeypatch.setattr(cet_ai, "_call", lambda *args: ({"chunks": ["Students can", "quiet room online."]}, "bad-model"))
    with pytest.raises(cet_ai.CETAIError) as invalid:
        cet_ai.generate("chunk", sentence, "sound")
    assert invalid.value.code == "AI_INVALID_OUTPUT"
    monkeypatch.setattr(cet_ai, "_call", lambda *args: ({"explanation": "错误 ɪ 音标", "keyPhrases": []}, "bad-model"))
    with pytest.raises(cet_ai.CETAIError):
        cet_ai.generate("explain", sentence, "meaning")
    for chunk in cet_service._local_chunks(sentence):
        assert len(chunk["text"].split()) <= 8
        assert chunk["timingSource"] == "estimated_alignment"

    monkeypatch.setattr(cet_ai, "_call", real_call)
    monkeypatch.setattr(ai_config, "AI_API_KEY", "")
    practice = create_practice(client, "missing-key")
    meaning = send_feedback(client, play_current(client, practice, "missing-key-play"), "meaning", "missing-key-feedback")
    response = next_step(client, meaning.json(), "missing-key-request")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_NOT_CONFIGURED"
    local = client.post(
        f"/api/cet/practices/{practice['id']}/local",
        json=practice_body(client, meaning.json(), actionId="missing-key-local", action="replay"),
    )
    assert local.status_code == 200
