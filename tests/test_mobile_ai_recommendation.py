"""Exercise recommendation generation and caching with an isolated lesson."""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db
from webapp.fastapi_routes import v2_lessons


def test_recommendation_generated_validated_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_USER_DB", str(tmp_path / "recommendation.db"))
    db.init_db()
    lesson = db.create_v2_lesson(source_type="developmentFixture", source_url="fixture://mobile-ai", title="Example")
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 0, "start": 0, "end": 3, "text": "Students can reserve a quiet room online."},
    ])
    monkeypatch.setattr(v2_lessons, "_begin_billed", lambda *a, **kw: (None, None))
    monkeypatch.setattr(v2_lessons.credit_meter, "settle_sync", lambda *a, **kw: None)
    monkeypatch.setattr(v2_lessons.credit_meter, "release_sync", lambda *a, **kw: None)
    calls = []
    def generate(prompt):
        calls.append(prompt)
        return {"words": [{"word": "reserve", "reason": "Useful verb"}, {"word": "absent", "reason": "Not in text"}], "patterns": []}
    monkeypatch.setattr(v2_lessons, "_request_pattern_json", generate)
    app = FastAPI()
    app.include_router(v2_lessons.router)
    with TestClient(app) as client:
        url = f"/api/v2/lessons/{lesson['id']}/ai-recommendations"
        first = client.post(url + "/refresh", json={"regenerate": False})
        assert first.status_code == 200, first.text
        assert [word["word"] for word in first.json()["recommendation"]["words"]] == ["reserve"]
        saved = client.get(url).json()["recommendation"]
        assert saved == first.json()["recommendation"]
        again = client.post(url + "/refresh", json={"regenerate": False})
        assert again.json()["cached"] is True
        assert len(calls) == 1


def test_failed_generation_keeps_existing_recommendations(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_USER_DB", str(tmp_path / "failure.db"))
    db.init_db()
    lesson = db.create_v2_lesson(source_type="developmentFixture", source_url="fixture://mobile-ai-failure", title="Example")
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 0, "start": 0, "end": 3, "text": "Students can reserve a quiet room online."},
    ])
    existing = {"words": [{"word": "reserve", "reason": "Useful verb"}], "patterns": []}
    db.save_v2_lesson_ai_recommendation(lesson["id"], existing, model="test")
    monkeypatch.setattr(v2_lessons, "_begin_billed", lambda *a, **kw: (None, None))
    released = []
    monkeypatch.setattr(v2_lessons.credit_meter, "release_sync", lambda *a, **kw: released.append(kw))
    def fail(_prompt):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(v2_lessons, "_request_pattern_json", fail)
    app = FastAPI()
    app.include_router(v2_lessons.router)
    with TestClient(app) as client:
        url = f"/api/v2/lessons/{lesson['id']}/ai-recommendations"
        response = client.post(url + "/refresh", json={"regenerate": True})
        assert response.status_code == 500
        assert "provider unavailable" in response.json()["error"]
        assert client.get(url).json()["recommendation"] == existing
        assert released
