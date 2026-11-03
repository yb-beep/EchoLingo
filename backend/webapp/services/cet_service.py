"""CET listening black-box business services backed by EchoLingo's SQLite database."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from fastapi import HTTPException

_FEEDBACK = {"sound", "meaning", "heard"}
_MATERIAL_RE = re.compile(r"^v2-(\d+)$")
_SENTENCE_RE = re.compile(r"^v2-(\d+)-(\d+)$")
_initialized: set[Path] = set()
_init_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error(status: int, code: str, message: str, **extra: Any) -> None:
    raise HTTPException(status, {"code": code, "message": message, **extra})


def init_cet_schema(path: Path | None = None) -> None:
    """Run idempotent CET migrations and recover interrupted model requests."""
    target = Path(path) if path is not None else db.current_db_path()
    with _init_lock:
        token = db.set_current_db_path(target) if path is not None else None
        try:
            with db._db() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS cet_questions (
                        id TEXT PRIMARY KEY,
                        material_id TEXT NOT NULL,
                        material_version TEXT NOT NULL,
                        question_number INTEGER NOT NULL,
                        prompt TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        correct_answer TEXT,
                        answer_source TEXT NOT NULL DEFAULT '',
                        evidence_sentence_id TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(material_id, question_number)
                    );
                    CREATE TABLE IF NOT EXISTS cet_attempts (
                        id TEXT PRIMARY KEY,
                        material_id TEXT NOT NULL,
                        material_version TEXT NOT NULL,
                        listening_session_id TEXT NOT NULL DEFAULT '',
                        score INTEGER NOT NULL,
                        graded_count INTEGER NOT NULL,
                        unknown_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cet_attempt_answers (
                        attempt_id TEXT NOT NULL REFERENCES cet_attempts(id) ON DELETE CASCADE,
                        question_id TEXT NOT NULL REFERENCES cet_questions(id),
                        answer TEXT NOT NULL DEFAULT '',
                        correct_answer TEXT,
                        status TEXT NOT NULL,
                        is_correct INTEGER,
                        answer_source TEXT NOT NULL DEFAULT '',
                        evidence_sentence_id TEXT,
                        PRIMARY KEY(attempt_id, question_id)
                    );
                    CREATE TABLE IF NOT EXISTS cet_marks (
                        id TEXT PRIMARY KEY,
                        listening_session_id TEXT NOT NULL,
                        material_id TEXT NOT NULL,
                        material_version TEXT NOT NULL,
                        raw_seconds REAL NOT NULL,
                        adjusted_seconds REAL NOT NULL,
                        speed REAL NOT NULL DEFAULT 1,
                        reaction_delay_seconds REAL NOT NULL DEFAULT 0.6,
                        suggested_sentence_id TEXT NOT NULL,
                        confirmed_sentence_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_cet_marks_material
                        ON cet_marks(material_id, listening_session_id, raw_seconds);
                    CREATE TABLE IF NOT EXISTS cet_practices (
                        id TEXT PRIMARY KEY,
                        mark_id TEXT NOT NULL UNIQUE REFERENCES cet_marks(id),
                        material_id TEXT NOT NULL,
                        material_version TEXT NOT NULL,
                        sentence_id TEXT NOT NULL,
                        sentence_json TEXT NOT NULL,
                        audio_url TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        current_step_id TEXT,
                        revision INTEGER NOT NULL DEFAULT 0,
                        effective_ms INTEGER NOT NULL DEFAULT 0,
                        latest_feedback TEXT,
                        feedback_source TEXT NOT NULL DEFAULT '',
                        used_text INTEGER NOT NULL DEFAULT 0,
                        transfer_status TEXT NOT NULL DEFAULT '',
                        ai_request_id TEXT,
                        completion_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS cet_practice_steps (
                        id TEXT PRIMARY KEY,
                        practice_id TEXT NOT NULL REFERENCES cet_practices(id) ON DELETE CASCADE,
                        sequence INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        source TEXT NOT NULL,
                        start_seconds REAL,
                        end_seconds REAL,
                        speed REAL,
                        show_text INTEGER NOT NULL DEFAULT 0,
                        feedback TEXT,
                        candidate_id TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(practice_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS cet_practice_events (
                        id TEXT NOT NULL,
                        practice_id TEXT NOT NULL REFERENCES cet_practices(id) ON DELETE CASCADE,
                        step_id TEXT,
                        material_version TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        audio_time REAL,
                        speed REAL,
                        active_ms INTEGER NOT NULL DEFAULT 0,
                        foreground INTEGER NOT NULL DEFAULT 1,
                        consumed INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        applied_ms INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(practice_id, id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_cet_events_step
                        ON cet_practice_events(practice_id, step_id, event_type, consumed);
                    CREATE TABLE IF NOT EXISTS cet_ai_requests (
                        id TEXT PRIMARY KEY,
                        practice_id TEXT NOT NULL REFERENCES cet_practices(id) ON DELETE CASCADE,
                        step_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_cet_ai_practice
                        ON cet_ai_requests(practice_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS cet_transfer_candidates (
                        id TEXT PRIMARY KEY,
                        practice_id TEXT NOT NULL REFERENCES cet_practices(id) ON DELETE CASCADE,
                        original_sentence_id TEXT NOT NULL,
                        candidate_json TEXT NOT NULL,
                        question TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        correct_answer TEXT NOT NULL,
                        answer_source TEXT NOT NULL,
                        explanation TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(practice_id, original_sentence_id)
                    );
                    CREATE TABLE IF NOT EXISTS cet_review_items (
                        id TEXT PRIMARY KEY,
                        practice_id TEXT NOT NULL UNIQUE REFERENCES cet_practices(id),
                        material_id TEXT NOT NULL,
                        material_version TEXT NOT NULL,
                        sentence_id TEXT NOT NULL,
                        latest_feedback TEXT,
                        feedback_source TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        priority INTEGER NOT NULL DEFAULT 60,
                        used_text INTEGER NOT NULL DEFAULT 0,
                        transfer_status TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(material_id, material_version, sentence_id)
                    );
                    CREATE TABLE IF NOT EXISTS cet_reviews (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        budget_ms INTEGER NOT NULL,
                        effective_ms INTEGER NOT NULL DEFAULT 0,
                        current_position INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 0,
                        pause_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cet_review_entries (
                        id TEXT PRIMARY KEY,
                        review_id TEXT NOT NULL REFERENCES cet_reviews(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        item_id TEXT NOT NULL REFERENCES cet_review_items(id),
                        practice_id TEXT NOT NULL REFERENCES cet_practices(id),
                        status TEXT NOT NULL DEFAULT 'pending',
                        UNIQUE(review_id, position),
                        UNIQUE(review_id, item_id)
                    );
                    CREATE TABLE IF NOT EXISTS cet_review_actions (
                        id TEXT NOT NULL,
                        review_id TEXT NOT NULL REFERENCES cet_reviews(id) ON DELETE CASCADE,
                        action TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(review_id, id)
                    );
                    """
                )
                conn.execute(
                    "UPDATE cet_ai_requests SET status='failed',error_code='SERVER_RESTARTED',completed_at=? "
                    "WHERE status='pending'", (_now(),)
                )
                conn.execute(
                    "UPDATE cet_reviews SET status='paused',pause_count=pause_count+1,updated_at=? "
                    "WHERE status='active'", (_now(),)
                )
            _initialized.add(target)
        finally:
            if token is not None:
                db.reset_current_db_path(token)


@contextmanager
def _db():
    target = db.current_db_path()
    with _init_lock:
        if target not in _initialized:
            init_cet_schema(target)
    token = db.set_current_db_path(target)
    try:
        with db._db() as conn:
            yield conn
    finally:
        db.reset_current_db_path(token)


def _lesson_id(material_id: str) -> int:
    match = _MATERIAL_RE.fullmatch(material_id)
    if not match:
        _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", materialId=material_id)
    return int(match.group(1))


def _segments(lesson_id: int) -> list[dict[str, Any]]:
    return db.get_v2_subtitle_segments(lesson_id)


def _level(lesson: dict[str, Any]) -> str:
    try:
        tags = json.loads(lesson.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    normalized = {str(tag).strip().lower() for tag in tags}
    if "cet6" in normalized or "六级" in normalized:
        return "cet6"
    if "cet4" in normalized or "四级" in normalized:
        return "cet4"
    return "unspecified"


def _sentence_ref(material_id: str, version: str, segment: dict[str, Any]) -> dict[str, Any]:
    lesson_id = _lesson_id(material_id)
    return {
        "materialId": material_id,
        "materialVersion": version,
        "sentenceId": f"v2-{lesson_id}-{int(segment['index'])}",
        "startSeconds": float(segment["start"]),
        "endSeconds": float(segment["end"]),
        "text": str(segment["text"]),
        "timingSource": "echolingo.v2_subtitle_segments",
    }


def _material(lesson: dict[str, Any], include_sentences: bool = True) -> dict[str, Any]:
    lesson_id = int(lesson["id"])
    material_id = f"v2-{lesson_id}"
    segments = _segments(lesson_id)
    version_payload = {
        "mediaUrl": lesson.get("media_url") or "",
        "segments": segments,
    }
    version = "sha256-" + hashlib.sha256(_json(version_payload).encode()).hexdigest()[:24]
    sentences = [_sentence_ref(material_id, version, segment) for segment in segments]
    result = {
        "id": material_id,
        "version": version,
        "title": lesson.get("title") or f"听力材料 {lesson_id}",
        "level": _level(lesson),
        "audioUrl": lesson.get("media_url") or "",
        "durationSeconds": float(lesson.get("duration") or 0),
        "source": {
            "type": lesson.get("source_type") or "",
            "value": lesson.get("source_url") or "",
            "materialStore": "echolingo.v2_lessons",
        },
        "sentenceCount": len(sentences),
    }
    if include_sentences:
        result["sentences"] = sentences
    return result


def list_materials(level: str | None = None) -> list[dict[str, Any]]:
    if level is not None and level not in {"cet4", "cet6", "unspecified"}:
        _error(422, "INVALID_LEVEL", "level 仅支持 cet4、cet6 或 unspecified。")
    lessons = db.list_v2_lessons()
    materials = [_material(lesson, include_sentences=False) for lesson in lessons]
    return [item for item in materials if level is None or item["level"] == level]


def get_material(material_id: str) -> dict[str, Any]:
    lesson = db.get_v2_lesson(_lesson_id(material_id))
    if not lesson or int(lesson.get("archived") or 0):
        _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", materialId=material_id)
    return _material(lesson)


def _find_sentence(material: dict[str, Any], sentence_id: str) -> dict[str, Any]:
    for sentence in material["sentences"]:
        if sentence["sentenceId"] == sentence_id:
            return sentence
    _error(422, "SENTENCE_OUT_OF_RANGE", "句子不属于该材料版本。", sentenceId=sentence_id)


def _options(raw: str | list[Any]) -> list[dict[str, str]]:
    values = json.loads(raw) if isinstance(raw, str) else raw
    result = []
    for index, value in enumerate(values):
        option_id = chr(65 + index)
        if isinstance(value, dict):
            option_id = str(value.get("id") or option_id)
            text = str(value.get("text") or "").strip()
        else:
            text = str(value).strip()
        if text:
            result.append({"id": option_id, "text": text})
    return result


def list_questions(material_id: str) -> dict[str, Any]:
    material = get_material(material_id)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM cet_questions WHERE material_id=? AND material_version=? ORDER BY question_number",
            (material_id, material["version"]),
        ).fetchall()
    # Deliberately omit answers, sources and evidence before an attempt is submitted.
    questions = [
        {
            "id": row["id"],
            "questionNumber": row["question_number"],
            "prompt": row["prompt"],
            "options": _options(row["options_json"]),
        }
        for row in rows
    ]
    return {"materialId": material_id, "materialVersion": material["version"], "questions": questions}


def _attempt_payload(attempt_id: str) -> dict[str, Any]:
    with _db() as conn:
        attempt = conn.execute("SELECT * FROM cet_attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            _error(404, "ATTEMPT_NOT_FOUND", "作答记录不存在。", attemptId=attempt_id)
        rows = conn.execute(
            "SELECT a.*, q.question_number, q.prompt, q.options_json FROM cet_attempt_answers a "
            "JOIN cet_questions q ON q.id=a.question_id WHERE a.attempt_id=? ORDER BY q.question_number",
            (attempt_id,),
        ).fetchall()
    answers = []
    for row in rows:
        evidence = None
        if row["evidence_sentence_id"]:
            material = get_material(attempt["material_id"])
            if material["version"] == attempt["material_version"]:
                evidence = _find_sentence(material, row["evidence_sentence_id"])
            else:
                evidence = {
                    "materialId": attempt["material_id"],
                    "materialVersion": attempt["material_version"],
                    "sentenceId": row["evidence_sentence_id"],
                }
        answers.append(
            {
                "questionId": row["question_id"],
                "questionNumber": row["question_number"],
                "answer": row["answer"],
                "correctAnswer": row["correct_answer"],
                "status": row["status"],
                "isCorrect": None if row["is_correct"] is None else bool(row["is_correct"]),
                "answerSource": row["answer_source"],
                "evidence": evidence,
            }
        )
    return {
        "attemptId": attempt["id"],
        "materialId": attempt["material_id"],
        "materialVersion": attempt["material_version"],
        "listeningSessionId": attempt["listening_session_id"],
        "score": attempt["score"],
        "gradedCount": attempt["graded_count"],
        "unknownCount": attempt["unknown_count"],
        "answers": answers,
        "submittedAt": attempt["created_at"],
    }


def submit_attempt(body: dict[str, Any]) -> dict[str, Any]:
    attempt_id = str(body.get("attemptId") or "").strip()
    material_id = str(body.get("materialId") or "").strip()
    if not attempt_id or not material_id:
        _error(422, "ATTEMPT_ID_REQUIRED", "attemptId 和 materialId 不能为空。")
    with _db() as conn:
        existing = conn.execute("SELECT material_id FROM cet_attempts WHERE id=?", (attempt_id,)).fetchone()
    if existing:
        if existing["material_id"] != material_id:
            _error(409, "IDEMPOTENCY_CONFLICT", "attemptId 已用于另一份材料。")
        return _attempt_payload(attempt_id)
    material = get_material(material_id)
    if body.get("materialVersion") != material["version"]:
        _error(409, "MATERIAL_VERSION_CHANGED", "材料版本已变化，请刷新后重试。", currentVersion=material["version"])
    question_data = list_questions(material_id)["questions"]
    questions = {item["id"]: item for item in question_data}
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, list):
        _error(422, "ANSWERS_REQUIRED", "answers 必须是数组。")
    answers: dict[str, str] = {}
    for item in raw_answers:
        if not isinstance(item, dict) or str(item.get("questionId") or "") not in questions:
            _error(422, "UNKNOWN_QUESTION", "作答包含不存在的题目。")
        question_id = str(item["questionId"])
        if question_id in answers:
            _error(422, "DUPLICATE_ANSWER", "同一题目不能重复作答。", questionId=question_id)
        answers[question_id] = str(item.get("answer") or "").strip()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM cet_questions WHERE material_id=? AND material_version=? ORDER BY question_number",
            (material_id, material["version"]),
        ).fetchall()
        score = graded = unknown = 0
        answer_rows = []
        for row in rows:
            answer = answers.get(row["id"], "")
            correct = row["correct_answer"]
            if correct is None or not str(correct).strip():
                status, is_correct = "unknown", None
                unknown += 1
            else:
                graded += 1
                is_correct = answer == str(correct)
                status = "correct" if is_correct else "incorrect"
                score += int(is_correct)
            answer_rows.append(
                (attempt_id, row["id"], answer, correct, status, is_correct,
                 row["answer_source"], row["evidence_sentence_id"])
            )
        now = _now()
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_attempts(id,material_id,material_version,listening_session_id,score,graded_count,unknown_count,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, material_id, material["version"], str(body.get("listeningSessionId") or ""), score, graded, unknown, now),
        )
        if not inserted.rowcount:
            existing = conn.execute("SELECT material_id FROM cet_attempts WHERE id=?", (attempt_id,)).fetchone()
            if existing["material_id"] != material_id:
                _error(409, "IDEMPOTENCY_CONFLICT", "attemptId 已用于另一份材料。")
            return _attempt_payload(attempt_id)
        conn.executemany(
            "INSERT INTO cet_attempt_answers(attempt_id,question_id,answer,correct_answer,status,is_correct,answer_source,evidence_sentence_id) "
            "VALUES(?,?,?,?,?,?,?,?)", answer_rows,
        )
    return _attempt_payload(attempt_id)


def _mark_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "listeningSessionId": row["listening_session_id"],
        "materialId": row["material_id"],
        "materialVersion": row["material_version"],
        "rawSeconds": row["raw_seconds"],
        "adjustedSeconds": row["adjusted_seconds"],
        "speed": row["speed"],
        "reactionDelaySeconds": row["reaction_delay_seconds"],
        "suggestedSentenceId": row["suggested_sentence_id"],
        "suggestionSource": "estimated_alignment",
        "confirmedSentenceId": row["confirmed_sentence_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _suggest_sentence(material: dict[str, Any], seconds: float) -> dict[str, Any]:
    sentences = material["sentences"]
    if not sentences:
        _error(409, "TIMELINE_UNAVAILABLE", "该材料没有可用句子时间轴。")
    return min(
        sentences,
        key=lambda sentence: (
            0.0 if sentence["startSeconds"] <= seconds <= sentence["endSeconds"]
            else min(abs(seconds - sentence["startSeconds"]), abs(seconds - sentence["endSeconds"])),
            int(sentence["sentenceId"].rsplit("-", 1)[1]),
        ),
    )


def create_mark(body: dict[str, Any]) -> dict[str, Any]:
    mark_id = str(body.get("markId") or body.get("id") or "").strip()
    material_id = str(body.get("materialId") or "").strip()
    session_id = str(body.get("listeningSessionId") or "").strip()
    if not mark_id or not material_id or not session_id:
        _error(422, "MARK_ID_REQUIRED", "markId、materialId 和 listeningSessionId 不能为空。")
    with _db() as conn:
        existing = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
    if existing:
        if existing["material_id"] != material_id:
            _error(409, "IDEMPOTENCY_CONFLICT", "markId 已用于另一份材料。")
        return _mark_payload(existing)
    try:
        seconds = float(body.get("rawSeconds"))
        speed = float(body.get("speed", 1.0))
        reaction_delay = float(body.get("reactionDelaySeconds", 0.6))
    except (TypeError, ValueError):
        _error(422, "INVALID_AUDIO_TIME", "rawSeconds、speed 和 reactionDelaySeconds 必须是数字。")
    if (
        not all(math.isfinite(value) for value in (seconds, speed, reaction_delay))
        or seconds < 0 or not 0 < speed <= 4 or not 0 <= reaction_delay <= 5
    ):
        _error(422, "INVALID_AUDIO_TIME", "按键时间、倍速或反应延迟无效。")
    material = get_material(material_id)
    if body.get("materialVersion") != material["version"]:
        _error(409, "MATERIAL_VERSION_CHANGED", "材料版本已变化，请刷新后重试。", currentVersion=material["version"])
    if material["durationSeconds"] > 0 and seconds > material["durationSeconds"] + 0.001:
        _error(422, "INVALID_AUDIO_TIME", "按键时间超出原音频范围。")
    adjusted = max(0.0, seconds - reaction_delay * speed)
    if material["durationSeconds"] > 0:
        adjusted = min(adjusted, material["durationSeconds"])
    suggested_id = str(body.get("suggestedSentenceId") or "").strip()
    suggested = _find_sentence(material, suggested_id) if suggested_id else _suggest_sentence(material, adjusted)
    now = _now()
    with _db() as conn:
        nearby = conn.execute(
            "SELECT * FROM cet_marks WHERE listening_session_id=? AND material_id=? AND material_version=? "
            "AND ABS(raw_seconds-?)<=1.5 ORDER BY ABS(raw_seconds-?),created_at LIMIT 1",
            (session_id, material_id, material["version"], seconds, seconds),
        ).fetchone()
        if nearby:
            return _mark_payload(nearby)
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM cet_marks WHERE listening_session_id=?", (session_id,)
        ).fetchone()["count"]
        if count >= 2000:
            _error(409, "MARK_LIMIT_REACHED", "本次听音最多保存 2000 个标记。")
        conn.execute(
            "INSERT OR IGNORE INTO cet_marks(id,listening_session_id,material_id,material_version,raw_seconds,adjusted_seconds,speed,reaction_delay_seconds,suggested_sentence_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mark_id, session_id, material_id, material["version"], seconds, adjusted, speed, reaction_delay,
             suggested["sentenceId"], now, now),
        )
        row = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
    return _mark_payload(row)


def list_marks(material_id: str, listening_session_id: str | None = None) -> list[dict[str, Any]]:
    get_material(material_id)
    query = "SELECT * FROM cet_marks WHERE material_id=?"
    params: list[Any] = [material_id]
    if listening_session_id:
        query += " AND listening_session_id=?"
        params.append(listening_session_id)
    query += " ORDER BY raw_seconds, created_at, id"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_mark_payload(row) for row in rows]


def confirm_mark(mark_id: str, body: dict[str, Any]) -> dict[str, Any]:
    sentence_id = str(body.get("confirmedSentenceId") or "").strip()
    if not sentence_id:
        _error(422, "CONFIRMED_SENTENCE_REQUIRED", "confirmedSentenceId 不能为空。")
    with _db() as conn:
        row = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
        if not row:
            _error(404, "MARK_NOT_FOUND", "标记不存在。", markId=mark_id)
        material = get_material(row["material_id"])
        if row["material_version"] != material["version"]:
            _error(409, "MATERIAL_VERSION_CHANGED", "标记所属材料版本已变化。", currentVersion=material["version"])
        if body.get("materialVersion") not in (None, row["material_version"]):
            _error(409, "MATERIAL_VERSION_CHANGED", "请求材料版本与标记不一致。")
        attempt = conn.execute(
            "SELECT id FROM cet_attempts WHERE material_id=? AND listening_session_id=? ORDER BY created_at DESC LIMIT 1",
            (row["material_id"], row["listening_session_id"]),
        ).fetchone()
        if not attempt:
            _error(409, "ATTEMPT_REQUIRED", "请先交卷，再确认回听句子。")
        sentence = _find_sentence(material, sentence_id)
        suggested_index = int(row["suggested_sentence_id"].rsplit("-", 1)[1])
        confirmed_index = int(sentence_id.rsplit("-", 1)[1])
        if abs(confirmed_index - suggested_index) > 1:
            _error(422, "CONFIRMATION_OUT_OF_RANGE", "确认句子只能调整为建议句子的前一句或后一句。")
        conn.execute(
            "UPDATE cet_marks SET confirmed_sentence_id=?,updated_at=? WHERE id=?",
            (sentence["sentenceId"], _now(), mark_id),
        )
        updated = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
    return _mark_payload(updated)


def replace_questions_for_fixture(material_id: str, questions: list[dict[str, Any]]) -> None:
    """Idempotent development fixture writer; never used by production routes."""
    material = get_material(material_id)
    with _db() as conn:
        conn.execute("DELETE FROM cet_questions WHERE material_id=?", (material_id,))
        for question in questions:
            conn.execute(
                "INSERT INTO cet_questions(id,material_id,material_version,question_number,prompt,options_json,correct_answer,answer_source,evidence_sentence_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(question["id"]), material_id, material["version"], int(question["questionNumber"]),
                    str(question["prompt"]), _json(question["options"]), question.get("correctAnswer"),
                    str(question.get("answerSource") or "developmentFixture"), question.get("evidenceSentenceId"), _now(),
                ),
            )


def _practice_row(conn, practice_id: str):
    row = conn.execute("SELECT * FROM cet_practices WHERE id=?", (practice_id,)).fetchone()
    if not row:
        _error(404, "PRACTICE_NOT_FOUND", "练习不存在。", practiceId=practice_id)
    return row


def _step_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sequence": row["sequence"],
        "action": row["action"],
        "source": row["source"],
        "playRange": None if row["start_seconds"] is None else {
            "startSeconds": row["start_seconds"], "endSeconds": row["end_seconds"],
        },
        "speed": row["speed"],
        "showText": bool(row["show_text"]),
        "feedback": row["feedback"],
        "candidateId": row["candidate_id"],
        "data": json.loads(row["payload_json"] or "{}"),
        "createdAt": row["created_at"],
    }


def _latest_ai_request(conn, practice_id: str):
    row = conn.execute(
        "SELECT * FROM cet_ai_requests WHERE practice_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (practice_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "requestId": row["id"], "stepId": row["step_id"], "revision": row["revision"],
        "action": row["action"], "status": row["status"], "errorCode": row["error_code"],
        "model": row["model"], "createdAt": row["created_at"], "completedAt": row["completed_at"],
    }


def _practice_payload(practice_id: str) -> dict[str, Any]:
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        steps = conn.execute(
            "SELECT * FROM cet_practice_steps WHERE practice_id=? ORDER BY sequence", (practice_id,)
        ).fetchall()
        feedback_rows = conn.execute(
            "SELECT * FROM cet_practice_events WHERE practice_id=? AND event_type='feedback' ORDER BY created_at,id",
            (practice_id,),
        ).fetchall()
        transfer_rows = conn.execute(
            "SELECT * FROM cet_practice_events WHERE practice_id=? AND event_type='transfer_answer' ORDER BY created_at DESC,id DESC LIMIT 1",
            (practice_id,),
        ).fetchall()
        ai_request = _latest_ai_request(conn, practice_id)
    sentence = json.loads(practice["sentence_json"])
    current_step = next((row for row in steps if row["id"] == practice["current_step_id"]), None)
    feedback_history = [
        {
            "feedback": json.loads(row["payload_json"]).get("feedback"),
            "source": "user",
            "stepId": row["step_id"],
            "eventId": row["id"],
            "createdAt": row["created_at"],
        }
        for row in feedback_rows
    ]
    latest_transfer = None
    if transfer_rows:
        data = json.loads(transfer_rows[0]["payload_json"])
        latest_transfer = {
            "candidateId": data.get("candidateId"), "answer": data.get("answer"),
            "correctAnswer": data.get("correctAnswer"), "isCorrect": data.get("isCorrect"),
            "answerSource": data.get("answerSource"), "assisted": True,
        }
    outcome = "blocked" if practice["latest_feedback"] in {"sound", "meaning"} else "unresolved"
    if practice["latest_feedback"] == "heard" and practice["transfer_status"] in {"", "matched", "no_candidate"}:
        heard_row = feedback_rows[-1] if feedback_rows else None
        if heard_row:
            heard_step = next((row for row in steps if row["id"] == heard_row["step_id"]), None)
            if heard_step and heard_step["action"] == "replay" and not heard_step["show_text"]:
                outcome = "heard"
    return {
        "id": practice["id"], "markId": practice["mark_id"],
        "materialId": practice["material_id"], "materialVersion": practice["material_version"],
        "sentence": sentence, "audioUrl": practice["audio_url"],
        "status": practice["status"], "revision": practice["revision"],
        "currentStepId": practice["current_step_id"],
        "currentStep": _step_payload(current_step) if current_step else None,
        "steps": [_step_payload(row) for row in steps],
        "latestFeedback": practice["latest_feedback"], "feedbackSource": practice["feedback_source"],
        "feedbackHistory": feedback_history, "outcome": outcome,
        "usedText": bool(practice["used_text"]), "transferStatus": practice["transfer_status"],
        "lastTransferResult": latest_transfer, "effectiveMs": practice["effective_ms"],
        "aiRequest": ai_request, "createdAt": practice["created_at"],
        "updatedAt": practice["updated_at"], "completedAt": practice["completed_at"],
    }


def _new_step(
    conn, practice: Any, action: str, source: str, *, show_text: bool, speed: float = 1.0,
    payload: dict[str, Any] | None = None, candidate_id: str | None = None,
    sentence: dict[str, Any] | None = None, feedback: str | None = None,
) -> str:
    sentence = sentence or json.loads(practice["sentence_json"])
    step_id = uuid.uuid4().hex
    now = _now()
    updated = conn.execute(
        "UPDATE cet_practices SET revision=revision+1,updated_at=? "
        "WHERE id=? AND revision=? AND status='active'",
        (now, practice["id"], practice["revision"]),
    )
    if not updated.rowcount:
        _error(409, "STALE_RESPONSE", "练习已切换到新步骤，请刷新后重试。", practiceId=practice["id"])
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence),-1)+1 AS next FROM cet_practice_steps WHERE practice_id=?",
        (practice["id"],),
    ).fetchone()["next"]
    conn.execute(
        "INSERT INTO cet_practice_steps(id,practice_id,sequence,action,source,start_seconds,end_seconds,speed,show_text,feedback,candidate_id,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            step_id, practice["id"], sequence, action, source,
            sentence.get("startSeconds"), sentence.get("endSeconds"), speed, int(show_text), feedback,
            candidate_id, _json(payload or {}), now,
        ),
    )
    conn.execute(
        "UPDATE cet_practices SET current_step_id=?,used_text=MAX(used_text,?),updated_at=? WHERE id=?",
        (step_id, int(show_text), now, practice["id"]),
    )
    return step_id


def _cancel_pending_ai(conn, practice: Any, status: str = "cancelled") -> None:
    if practice["ai_request_id"]:
        conn.execute(
            "UPDATE cet_ai_requests SET status=?,completed_at=? WHERE id=? AND status='pending'",
            (status, _now(), practice["ai_request_id"]),
        )


def create_practice(body: dict[str, Any]) -> dict[str, Any]:
    practice_id = str(body.get("practiceId") or "").strip()
    mark_id = str(body.get("markId") or "").strip()
    if not practice_id or not mark_id:
        _error(422, "PRACTICE_ID_REQUIRED", "practiceId 和 markId 不能为空。")
    with _db() as conn:
        existing = conn.execute("SELECT id FROM cet_practices WHERE id=? OR mark_id=?", (practice_id, mark_id)).fetchone()
        if existing:
            return _practice_payload(existing["id"])
        mark = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
        if not mark:
            _error(404, "MARK_NOT_FOUND", "标记不存在。", markId=mark_id)
        if not mark["confirmed_sentence_id"]:
            _error(409, "CONFIRMATION_REQUIRED", "请先确认真正卡住的句子。")
        attempt = conn.execute(
            "SELECT id FROM cet_attempts WHERE material_id=? AND listening_session_id=? ORDER BY created_at DESC LIMIT 1",
            (mark["material_id"], mark["listening_session_id"]),
        ).fetchone()
        if not attempt:
            _error(409, "ATTEMPT_REQUIRED", "请先交卷，再进入回听带练。")
    material = get_material(mark["material_id"])
    if material["version"] != mark["material_version"]:
        _error(409, "MATERIAL_VERSION_CHANGED", "材料版本已变化，请重新确认句子。")
    sentence = _find_sentence(material, mark["confirmed_sentence_id"])
    now = _now()
    step_id = uuid.uuid4().hex
    with _db() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_practices(id,mark_id,material_id,material_version,sentence_id,sentence_json,audio_url,current_step_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (practice_id, mark_id, mark["material_id"], mark["material_version"], sentence["sentenceId"],
             _json(sentence), material["audioUrl"], step_id, now, now),
        )
        if not inserted.rowcount:
            existing = conn.execute("SELECT id FROM cet_practices WHERE id=? OR mark_id=?", (practice_id, mark_id)).fetchone()
            return _practice_payload(existing["id"])
        conn.execute(
            "INSERT INTO cet_practice_steps(id,practice_id,sequence,action,source,start_seconds,end_seconds,speed,show_text,payload_json,created_at) "
            "VALUES(?,?,0,'replay','system',?,?,1,0,?,?)",
            (step_id, practice_id, sentence["startSeconds"], sentence["endSeconds"], _json({"initial": True}), now),
        )
    return _practice_payload(practice_id)


def _local_chunks(sentence: dict[str, Any]) -> list[dict[str, Any]]:
    text = sentence["text"]
    matches = list(re.finditer(r"\S+", text))
    if not matches:
        return []
    group_size = max(1, math.ceil(len(matches) / max(1, math.ceil(len(matches) / 6))))
    chunks = []
    start = float(sentence["startSeconds"])
    duration = float(sentence["endSeconds"]) - start
    for offset in range(0, len(matches), group_size):
        group = matches[offset:offset + group_size]
        char_start, char_end = group[0].start(), group[-1].end()
        chunks.append({
            "text": text[char_start:char_end],
            "startSeconds": start + duration * char_start / max(len(text), 1),
            "endSeconds": start + duration * char_end / max(len(text), 1),
            "timingSource": "estimated_alignment",
        })
    if chunks:
        chunks[-1]["endSeconds"] = float(sentence["endSeconds"])
    return chunks


def local_practice_action(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "").strip()
    action_id = str(body.get("actionId") or "").strip()
    if action not in {"replay", "slow", "chunk", "explain"} or not action_id:
        _error(422, "INVALID_LOCAL_ACTION", "本地动作仅支持 replay、slow、chunk、explain，且需要 actionId。")
    with _db() as conn:
        duplicate = conn.execute(
            "SELECT id FROM cet_practice_events WHERE practice_id=? AND id=?", (practice_id, action_id)
        ).fetchone()
        if duplicate:
            return _practice_payload(practice_id)
        practice = _practice_row(conn, practice_id)
        if practice["status"] != "active":
            _error(409, "PRACTICE_COMPLETE", "练习已结束。")
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "步骤或材料版本已变化，请刷新后重试。")
        _cancel_pending_ai(conn, practice)
        payload: dict[str, Any] = {"source": "local"}
        sentence = json.loads(practice["sentence_json"])
        if action == "chunk":
            payload["chunks"] = _local_chunks(sentence)
            payload["timingSource"] = "estimated_alignment"
        elif action == "explain":
            payload.update({"explanation": "", "degraded": True, "text": sentence["text"]})
        _new_step(
            conn, practice, action, "local", show_text=action != "replay",
            speed=0.75 if action in {"slow", "chunk"} else 1.0, payload=payload,
        )
        conn.execute(
            "INSERT INTO cet_practice_events(id,practice_id,step_id,material_version,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,'local_action',?,?)",
            (action_id, practice_id, practice["current_step_id"], practice["material_version"], _json({"action": action}), _now()),
        )
    return _practice_payload(practice_id)


def _content_words(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "is", "are", "was", "were", "it", "that", "this", "for", "with", "can", "how"}
    return {word.lower() for word in re.findall(r"[A-Za-z]+", text) if len(word) > 2 and word.lower() not in stop}


def _transfer_candidate(conn, practice: Any) -> dict[str, Any] | None:
    original = json.loads(practice["sentence_json"])
    anchors = _content_words(original["text"])
    if not anchors:
        return None
    exposed = {row[0] for row in conn.execute("SELECT DISTINCT material_id FROM cet_attempts").fetchall()}
    exposed.update(row[0] for row in conn.execute("SELECT material_id FROM cet_practices").fetchall())
    exposed.update(
        row[0] for row in conn.execute(
            "SELECT DISTINCT json_extract(candidate_json,'$.materialId') FROM cet_transfer_candidates"
        ).fetchall() if row[0]
    )
    lesson_ids = [int(value.split("-", 1)[1]) for value in exposed if value.startswith("v2-")]
    exposed_sources: set[str] = set()
    if lesson_ids:
        placeholders = ",".join("?" for _ in lesson_ids)
        exposed_sources = {
            row[0] for row in conn.execute(
                f"SELECT DISTINCT source_url FROM v2_lessons WHERE id IN ({placeholders})", lesson_ids
            ).fetchall() if row[0]
        }
    rows = conn.execute(
        "SELECT s.lesson_id,s.idx,s.start,s.end,s.text,l.media_url,l.updated_at,l.source_url "
        "FROM v2_subtitle_segments s JOIN v2_lessons l ON l.id=s.lesson_id "
        "WHERE l.archived=0 AND l.media_url<>'' ORDER BY s.lesson_id,s.idx"
    ).fetchall()
    ranked = []
    for row in rows:
        material_id = f"v2-{row['lesson_id']}"
        sentence_id = f"{material_id}-{row['idx']}"
        if material_id in exposed or sentence_id == original["sentenceId"] or row["source_url"] in exposed_sources:
            continue
        candidate_text = str(row["text"])
        if candidate_text.strip().lower() == original["text"].strip().lower():
            continue
        overlap = anchors & _content_words(candidate_text)
        if not overlap:
            continue
        score = len(overlap) / max(len(anchors | _content_words(candidate_text)), 1)
        ranked.append((score, len(overlap), row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], -item[1], int(item[2]["lesson_id"]), int(item[2]["idx"])))
    _, _, row = ranked[0]
    material_id = f"v2-{row['lesson_id']}"
    lesson = db.get_v2_lesson(int(row["lesson_id"]))
    material_version = _material(lesson, include_sentences=False)["version"]
    return {
        "materialId": material_id,
        "materialVersion": material_version,
        "sentenceId": f"{material_id}-{row['idx']}",
        "startSeconds": float(row["start"]), "endSeconds": float(row["end"]),
        "text": row["text"], "audioUrl": row["media_url"],
        "source": {"type": "echolingo.v2_subtitle_segments", "unseen": True},
    }


def _next_action(conn, practice: Any, requested: str) -> str:
    if requested:
        return requested
    current = conn.execute("SELECT * FROM cet_practice_steps WHERE id=?", (practice["current_step_id"],)).fetchone()
    feedback = practice["latest_feedback"]
    if feedback == "meaning":
        return "explain"
    if feedback == "sound":
        previous_slow = conn.execute(
            "SELECT 1 FROM cet_practice_steps WHERE practice_id=? AND action IN ('slow','chunk') LIMIT 1",
            (practice["id"],),
        ).fetchone()
        return "chunk" if previous_slow else "slow"
    if feedback == "heard":
        if current and current["action"] == "replay" and not current["show_text"]:
            return "transfer"
        return "replay"
    return "replay"


def next_practice_step(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    request_id = str(body.get("requestId") or "").strip()
    if not request_id:
        _error(422, "REQUEST_ID_REQUIRED", "requestId 不能为空。")
    requested = str(body.get("action") or "").strip()
    if requested in {"replay", "slow"}:
        return local_practice_action(practice_id, {**body, "actionId": request_id, "action": requested})
    if requested and requested not in {"chunk", "explain", "transfer"}:
        _error(422, "INVALID_ACTION", "不支持的动作。")
    with _db() as conn:
        previous = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
        if previous:
            if previous["status"] == "succeeded":
                return _practice_payload(practice_id)
            _error(409 if previous["status"] == "pending" else 502, "AI_REQUEST_NOT_RETRYABLE", "该请求已处理，请使用新的 requestId 重试。")
        practice = _practice_row(conn, practice_id)
        if practice["status"] != "active":
            _error(409, "PRACTICE_COMPLETE", "练习已结束。")
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "步骤或材料版本已变化，请刷新后重试。")
        action = _next_action(conn, practice, requested)
        if action in {"replay", "slow"}:
            return local_practice_action(practice_id, {**body, "actionId": request_id, "action": action})
        sentence = json.loads(practice["sentence_json"])
        candidate = _transfer_candidate(conn, practice) if action == "transfer" else None
        revision = practice["revision"] + 1
        now = _now()
        request_data = {"sentence": sentence, "feedback": practice["latest_feedback"], "candidate": candidate}
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_ai_requests(id,practice_id,step_id,revision,action,status,request_json,created_at) "
            "VALUES(?,?,?,?,?,'pending',?,?)",
            (request_id, practice_id, practice["current_step_id"], revision, action, _json(request_data), now),
        )
        if not inserted.rowcount:
            conn.rollback()
            previous = conn.execute("SELECT status FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
            if previous["status"] == "succeeded":
                return _practice_payload(practice_id)
            _error(409, "AI_REQUEST_IN_PROGRESS", "相同模型请求正在处理或已失败，请查询状态或使用新 requestId。")
        _cancel_pending_ai(conn, practice)
        changed = conn.execute(
            "UPDATE cet_practices SET revision=?,ai_request_id=?,updated_at=? WHERE id=? AND revision=? AND status='active'",
            (revision, request_id, now, practice_id, practice["revision"]),
        )
        if not changed.rowcount:
            _error(409, "STALE_RESPONSE", "练习已变化，请刷新后重试。")
    try:
        if action == "transfer" and candidate is None:
            generated, model = {"noCandidate": True, "reason": "no_candidate"}, ""
        else:
            from webapp.services import cet_ai
            generated, model = cet_ai.generate(action, sentence, practice["latest_feedback"] or "", candidate)
    except cet_ai.CETAIError as exc:
        with _db() as conn:
            current = _practice_row(conn, practice_id)
            if current["ai_request_id"] == request_id and current["revision"] == revision:
                conn.execute(
                    "UPDATE cet_ai_requests SET status='failed',error_code=?,completed_at=? WHERE id=? AND status='pending'",
                    (exc.code, _now(), request_id),
                )
            else:
                conn.execute("UPDATE cet_ai_requests SET status='stale',completed_at=? WHERE id=?", (_now(), request_id))
                conn.commit()
                _error(409, "STALE_RESPONSE", "模型错误结果已过期，未改变当前练习。", practiceId=practice_id, requestId=request_id)
        _error(exc.status, exc.code, exc.message, practiceId=practice_id, requestId=request_id)
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        request = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
        if (
            practice["status"] != "active" or practice["revision"] != revision
            or practice["current_step_id"] != request["step_id"] or practice["ai_request_id"] != request_id
            or request["status"] != "pending"
        ):
            conn.execute("UPDATE cet_ai_requests SET status='stale',completed_at=? WHERE id=?", (_now(), request_id))
            conn.commit()
            _error(409, "STALE_RESPONSE", "模型结果已过期，未覆盖当前练习。", practiceId=practice_id, requestId=request_id)
        candidate_id = None
        sentence_for_step = sentence
        public_data = generated
        if action == "transfer" and not generated.get("noCandidate"):
            candidate_id = "transfer-" + hashlib.sha256(f"{practice_id}:{candidate['sentenceId']}".encode()).hexdigest()[:20]
            answer_source = f"ai:{model}:{request_id}"
            conn.execute(
                "INSERT INTO cet_transfer_candidates(id,practice_id,original_sentence_id,candidate_json,question,options_json,correct_answer,answer_source,explanation,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, practice_id, sentence["sentenceId"], _json(candidate), generated["question"],
                 _json(generated["options"]), generated["correctAnswer"], answer_source, generated["explanation"], _now()),
            )
            sentence_for_step = candidate
            public_data = {
                "candidate": candidate, "question": generated["question"], "options": generated["options"],
                "assisted": True, "source": "ai",
            }
        elif action == "transfer" and generated.get("noCandidate"):
            public_data = generated
        _new_step(
            conn, practice, "finish" if generated.get("noCandidate") else action,
            "local" if generated.get("noCandidate") else "ai",
            show_text=action in {"chunk", "explain"}, speed=0.75 if action == "chunk" else 1.0,
            payload=public_data, candidate_id=candidate_id, sentence=sentence_for_step,
        )
        if action == "transfer":
            conn.execute(
                "UPDATE cet_practices SET transfer_status=? WHERE id=?",
                ("no_candidate" if generated.get("noCandidate") else "presented", practice_id),
            )
        conn.execute(
            "UPDATE cet_ai_requests SET status='succeeded',response_json=?,model=?,completed_at=? WHERE id=?",
            (_json(generated), model, _now(), request_id),
        )
    return _practice_payload(practice_id)


def _review_timing_context(conn, practice_id: str, review_id: str | None):
    if review_id:
        review = conn.execute("SELECT * FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
        if not review:
            _error(404, "REVIEW_NOT_FOUND", "复习会话不存在。", reviewId=review_id)
        entry = conn.execute(
            "SELECT * FROM cet_review_entries WHERE review_id=? AND position=?", (review_id, review["current_position"])
        ).fetchone()
        if not entry or entry["practice_id"] != practice_id:
            _error(409, "REVIEW_PRACTICE_MISMATCH", "事件不属于当前复习条目。")
        return review
    return conn.execute(
        "SELECT r.* FROM cet_reviews r JOIN cet_review_entries e ON e.review_id=r.id AND e.position=r.current_position "
        "WHERE e.practice_id=? AND r.status IN ('active','paused') ORDER BY r.updated_at DESC LIMIT 1",
        (practice_id,),
    ).fetchone()


def _is_visible(conn, practice_id: str) -> bool:
    row = conn.execute(
        "SELECT payload_json FROM cet_practice_events WHERE practice_id=? AND event_type='visibility' ORDER BY rowid DESC LIMIT 1",
        (practice_id,),
    ).fetchone()
    return True if not row else bool(json.loads(row["payload_json"]).get("visible"))


def _apply_review_time(conn, review: Any, applied_ms: int) -> None:
    if not review or not applied_ms or review["status"] != "active":
        return
    remaining = max(0, review["budget_ms"] - review["effective_ms"])
    accepted = min(remaining, applied_ms)
    effective = review["effective_ms"] + accepted
    conn.execute(
        "UPDATE cet_reviews SET effective_ms=?,status=?,updated_at=? WHERE id=?",
        (effective, "complete" if effective >= review["budget_ms"] else "active", _now(), review["id"]),
    )


def record_practice_event(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    event_id = str(body.get("eventId") or "").strip()
    event_type = str(body.get("type") or body.get("eventType") or "").strip()
    allowed = {"playback_start", "playback_end", "playback_interrupt", "activity_heartbeat", "visibility", "feedback", "transfer_answer"}
    if not event_id or event_type not in allowed:
        _error(422, "INVALID_EVENT", "eventId 或事件类型无效。", allowedTypes=sorted(allowed))
    with _db() as conn:
        duplicate = conn.execute(
            "SELECT * FROM cet_practice_events WHERE practice_id=? AND id=?", (practice_id, event_id)
        ).fetchone()
        if duplicate:
            result = _practice_payload(practice_id)
            result["event"] = {"eventId": event_id, "duplicate": True, "appliedMs": duplicate["applied_ms"]}
            return result
        practice = _practice_row(conn, practice_id)
        if practice["status"] != "active":
            _error(409, "PRACTICE_COMPLETE", "练习已结束，不能新增事件。")
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "事件步骤或材料版本已过期。")
        step = conn.execute("SELECT * FROM cet_practice_steps WHERE id=?", (practice["current_step_id"],)).fetchone()
        review = _review_timing_context(conn, practice_id, body.get("reviewId"))
        visible = _is_visible(conn, practice_id)
        latest_ai = conn.execute(
            "SELECT status FROM cet_ai_requests WHERE id=?", (practice["ai_request_id"],)
        ).fetchone() if practice["ai_request_id"] else None
        ai_waiting = bool(latest_ai and latest_ai["status"] == "pending")
        timing_allowed = visible and not ai_waiting and (not review or review["status"] == "active")
        foreground = bool(body.get("foreground", True)) and visible
        active_ms = body.get("activeMs", 0) or 0
        try:
            active_ms = int(active_ms)
        except (TypeError, ValueError):
            _error(422, "INVALID_ACTIVE_TIME", "activeMs 必须是非负整数。")
        if active_ms < 0:
            _error(422, "INVALID_ACTIVE_TIME", "activeMs 必须是非负整数。")
        payload: dict[str, Any] = {}
        applied_ms = 0
        audio_time = body.get("audioTime")
        speed = body.get("speed", 1.0)
        if event_type.startswith("playback_"):
            try:
                audio_time = float(audio_time)
                speed = float(speed)
            except (TypeError, ValueError):
                _error(422, "INVALID_PLAYBACK_EVENT", "audioTime 和 speed 必须是数字。")
            if not math.isfinite(audio_time) or not math.isfinite(speed) or speed <= 0:
                _error(422, "INVALID_PLAYBACK_EVENT", "audioTime 或 speed 无效。")
            if step["action"] == "finish":
                _error(409, "PLAYBACK_NOT_ALLOWED", "结束步骤不能新增播放事件。")
            if step["start_seconds"] is None or step["speed"] is None or abs(speed - float(step["speed"])) > 0.001:
                _error(422, "PLAYBACK_SPEED_MISMATCH", "播放速度必须匹配当前带练步骤。")
            if audio_time < float(step["start_seconds"]) - 0.25 or audio_time > float(step["end_seconds"]) + 0.7:
                _error(422, "PLAYBACK_OUT_OF_RANGE", "音频时间超出当前步骤范围。")
        if event_type == "playback_start":
            if active_ms:
                _error(422, "INVALID_ACTIVE_TIME", "播放开始事件不能携带 activeMs。")
            payload = {"playedToEnd": False}
        elif event_type in {"playback_end", "playback_interrupt"}:
            start = conn.execute(
                "SELECT * FROM cet_practice_events WHERE practice_id=? AND step_id=? AND event_type='playback_start' AND consumed=0 "
                "ORDER BY rowid DESC LIMIT 1",
                (practice_id, practice["current_step_id"]),
            ).fetchone()
            if not start:
                _error(422, "PLAYBACK_START_REQUIRED", "缺少匹配的播放开始事件。")
            foreground = foreground and bool(start["foreground"])
            theoretical = max(0, round((float(step["end_seconds"]) - float(start["audio_time"])) * 1000 / speed))
            if active_ms > theoretical + 700:
                _error(422, "INVALID_ACTIVE_TIME", "有效播放用时超过音频事件允许范围。")
            played_to_end = (
                event_type == "playback_end"
                and float(step["end_seconds"]) - 0.25 <= audio_time <= float(step["end_seconds"]) + 0.7
                and start["audio_time"] <= float(step["start_seconds"]) + 0.25
                and active_ms >= max(0, theoretical - 400)
            )
            payload = {"playedToEnd": played_to_end, "startEventId": start["id"]}
            if timing_allowed and foreground:
                applied_ms = min(active_ms, theoretical + 250)
            conn.execute("UPDATE cet_practice_events SET consumed=1 WHERE practice_id=? AND id=?", (practice_id, start["id"]))
        elif event_type == "activity_heartbeat":
            if not 1 <= active_ms <= 5000:
                _error(422, "INVALID_ACTIVE_TIME", "前台学习心跳 activeMs 必须为 1—5000。")
            if timing_allowed and foreground:
                applied_ms = active_ms
        elif event_type == "visibility":
            if not isinstance(body.get("visible"), bool):
                _error(422, "VISIBILITY_REQUIRED", "visibility 事件需要布尔 visible。")
            foreground = body["visible"]
            payload = {"visible": body["visible"]}
            if not body["visible"]:
                conn.execute(
                    "UPDATE cet_practice_events SET consumed=1 WHERE practice_id=? AND event_type='playback_start' AND consumed=0",
                    (practice_id,),
                )
        elif event_type == "feedback":
            feedback = str(body.get("feedback") or "").strip()
            if feedback not in _FEEDBACK:
                _error(422, "INVALID_FEEDBACK", "feedback 仅支持 sound、meaning、heard。")
            played = conn.execute(
                "SELECT 1 FROM cet_practice_events WHERE practice_id=? AND step_id=? AND event_type='playback_end' "
                "AND json_extract(payload_json,'$.playedToEnd')=1 LIMIT 1",
                (practice_id, practice["current_step_id"]),
            ).fetchone()
            if step["action"] != "explain" and not played:
                _error(409, "PLAYBACK_REQUIRED", "请先完整播放当前步骤，再提交反馈。")
            payload = {"feedback": feedback}
            _cancel_pending_ai(conn, practice)
            conn.execute(
                "UPDATE cet_practices SET latest_feedback=?,feedback_source='user',revision=revision+1,updated_at=? WHERE id=?",
                (feedback, _now(), practice_id),
            )
        elif event_type == "transfer_answer":
            if step["action"] != "transfer" or not step["candidate_id"]:
                _error(409, "TRANSFER_NOT_ACTIVE", "当前步骤不是换句检查。")
            played = conn.execute(
                "SELECT 1 FROM cet_practice_events WHERE practice_id=? AND step_id=? AND event_type='playback_end' "
                "AND json_extract(payload_json,'$.playedToEnd')=1 LIMIT 1",
                (practice_id, practice["current_step_id"]),
            ).fetchone()
            if not played:
                _error(409, "PLAYBACK_REQUIRED", "请先完整播放换句原声。")
            candidate = conn.execute(
                "SELECT * FROM cet_transfer_candidates WHERE id=? AND practice_id=?", (step["candidate_id"], practice_id)
            ).fetchone()
            answer = str(body.get("answer") or "").strip()
            options = _options(candidate["options_json"])
            if answer not in {option["id"] for option in options}:
                _error(422, "UNKNOWN_TRANSFER_ANSWER", "换句答案不在选项中。")
            correct = answer == candidate["correct_answer"]
            payload = {
                "candidateId": candidate["id"], "answer": answer, "correctAnswer": candidate["correct_answer"],
                "isCorrect": correct, "answerSource": candidate["answer_source"], "explanation": candidate["explanation"],
            }
            conn.execute(
                "UPDATE cet_practices SET transfer_status=?,revision=revision+1,updated_at=? WHERE id=?",
                ("matched" if correct else "failed", _now(), practice_id),
            )
        now = _now()
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_practice_events(id,practice_id,step_id,material_version,event_type,audio_time,speed,active_ms,foreground,payload_json,applied_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, practice_id, practice["current_step_id"], practice["material_version"], event_type,
             audio_time if event_type.startswith("playback_") else None,
             speed if event_type.startswith("playback_") else None, active_ms, int(foreground), _json(payload), applied_ms, now),
        )
        if not inserted.rowcount:
            conn.rollback()
            duplicate = conn.execute(
                "SELECT * FROM cet_practice_events WHERE practice_id=? AND id=?", (practice_id, event_id)
            ).fetchone()
            result = _practice_payload(practice_id)
            result["event"] = {"eventId": event_id, "duplicate": True, "appliedMs": duplicate["applied_ms"]}
            return result
        if applied_ms:
            conn.execute("UPDATE cet_practices SET effective_ms=effective_ms+?,updated_at=? WHERE id=?", (applied_ms, now, practice_id))
            _apply_review_time(conn, review, applied_ms)
    result = _practice_payload(practice_id)
    result["event"] = {"eventId": event_id, "duplicate": False, "appliedMs": applied_ms, **payload}
    return result


def _review_priority(practice: Any) -> int:
    if practice["status"] == "active":
        return 100
    if practice["transfer_status"] == "failed":
        return 90
    if practice["latest_feedback"] in {"sound", "meaning"}:
        return 80
    if practice["used_text"]:
        return 65
    if practice["latest_feedback"] == "heard" and practice["transfer_status"] in {"", "matched", "no_candidate"}:
        return 30
    return 60


def _review_item_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "practiceId": row["practice_id"],
        "materialId": row["material_id"], "materialVersion": row["material_version"],
        "sentence": json.loads(row["sentence_json"]), "audioUrl": row["audio_url"],
        "latestFeedback": row["latest_feedback"], "feedbackSource": row["feedback_source"],
        "status": row["status"], "priority": row["priority"], "usedText": bool(row["used_text"]),
        "transferStatus": row["transfer_status"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def _save_review_item(conn, practice: Any) -> dict[str, Any]:
    now = _now()
    item_id = "item-" + hashlib.sha256(
        f"{practice['material_id']}:{practice['material_version']}:{practice['sentence_id']}".encode()
    ).hexdigest()[:20]
    priority = _review_priority(practice)
    existing = conn.execute("SELECT * FROM cet_review_items WHERE id=?", (item_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE cet_review_items SET practice_id=?,latest_feedback=?,feedback_source=?,status='pending',priority=?,used_text=?,transfer_status=?,updated_at=? WHERE id=?",
            (practice["id"], practice["latest_feedback"], practice["feedback_source"], priority,
             practice["used_text"], practice["transfer_status"], now, item_id),
        )
        conn.execute("UPDATE cet_review_entries SET practice_id=? WHERE item_id=?", (practice["id"], item_id))
    else:
        conn.execute(
            "INSERT INTO cet_review_items(id,practice_id,material_id,material_version,sentence_id,latest_feedback,feedback_source,status,priority,used_text,transfer_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
            (item_id, practice["id"], practice["material_id"], practice["material_version"], practice["sentence_id"],
             practice["latest_feedback"], practice["feedback_source"], priority, practice["used_text"],
             practice["transfer_status"], now, now),
        )
    return conn.execute(
        "SELECT i.*,p.sentence_json,p.audio_url FROM cet_review_items i JOIN cet_practices p ON p.id=i.practice_id WHERE i.id=?",
        (item_id,),
    ).fetchone()


def finish_practice(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    finish_id = str(body.get("finishId") or "").strip()
    if not finish_id:
        _error(422, "FINISH_ID_REQUIRED", "finishId 不能为空。")
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        if practice["status"] == "complete":
            item = conn.execute(
                "SELECT i.*,p.sentence_json,p.audio_url FROM cet_review_items i JOIN cet_practices p ON p.id=i.practice_id "
                "WHERE i.material_id=? AND i.material_version=? AND i.sentence_id=?",
                (practice["material_id"], practice["material_version"], practice["sentence_id"]),
            ).fetchone()
            return {"practice": _practice_payload(practice_id), "reviewItem": _review_item_payload(item)}
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "步骤或材料版本已变化，请刷新后重试。")
        _cancel_pending_ai(conn, practice)
        _new_step(conn, practice, "finish", "user", show_text=False, payload={"finishId": finish_id})
        now = _now()
        conn.execute(
            "UPDATE cet_practices SET status='complete',completion_id=?,completed_at=?,updated_at=? WHERE id=?",
            (finish_id, now, now, practice_id),
        )
        practice = _practice_row(conn, practice_id)
        item = _save_review_item(conn, practice)
        item_payload = _review_item_payload(item)
    return {"practice": _practice_payload(practice_id), "reviewItem": item_payload}


def list_review_items() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT i.*,p.sentence_json,p.audio_url FROM cet_review_items i JOIN cet_practices p ON p.id=i.practice_id "
            "ORDER BY i.priority DESC,i.updated_at DESC,i.id"
        ).fetchall()
    return [_review_item_payload(row) for row in rows]


def _review_payload(review_id: str, include_practice: bool = True) -> dict[str, Any]:
    with _db() as conn:
        review = conn.execute("SELECT * FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
        if not review:
            _error(404, "REVIEW_NOT_FOUND", "复习会话不存在。", reviewId=review_id)
        rows = conn.execute(
            "SELECT e.id AS entry_id,e.position,e.status AS entry_status,i.*,p.sentence_json,p.audio_url "
            "FROM cet_review_entries e JOIN cet_review_items i ON i.id=e.item_id "
            "JOIN cet_practices p ON p.id=e.practice_id WHERE e.review_id=? ORDER BY e.position",
            (review_id,),
        ).fetchall()
    entries = []
    current_practice_id = None
    for row in rows:
        item = _review_item_payload(row)
        if row["position"] == review["current_position"] and review["status"] != "complete":
            current_practice_id = row["practice_id"]
        entries.append({
            "id": row["entry_id"], "position": row["position"], "status": row["entry_status"],
            "practiceId": row["practice_id"], "item": item,
        })
    result = {
        "id": review["id"], "status": review["status"], "budgetMs": review["budget_ms"],
        "effectiveMs": review["effective_ms"], "remainingMs": max(0, review["budget_ms"] - review["effective_ms"]),
        "currentPosition": review["current_position"], "revision": review["revision"],
        "pauseCount": review["pause_count"], "entries": entries,
        "currentEntryId": next((entry["id"] for entry in entries if entry["position"] == review["current_position"]), None)
        if review["status"] != "complete" else None,
        "createdAt": review["created_at"], "updatedAt": review["updated_at"],
    }
    if include_practice:
        result["currentPractice"] = _practice_payload(current_practice_id) if current_practice_id else None
    return result


def create_review(body: dict[str, Any]) -> dict[str, Any]:
    review_id = str(body.get("reviewId") or "").strip()
    if not review_id:
        _error(422, "REVIEW_ID_REQUIRED", "reviewId 不能为空。")
    try:
        budget = int(body.get("budgetMs", 600000))
    except (TypeError, ValueError):
        _error(422, "INVALID_REVIEW_BUDGET", "budgetMs 必须为 1—600000。")
    if not 1 <= budget <= 600000:
        _error(422, "INVALID_REVIEW_BUDGET", "budgetMs 必须为 1—600000。")
    with _db() as conn:
        existing = conn.execute("SELECT id FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
        if existing:
            return _review_payload(review_id)
        requested_ids = body.get("itemIds")
        if requested_ids is not None and (not isinstance(requested_ids, list) or not 1 <= len(requested_ids) <= 3):
            _error(422, "INVALID_REVIEW_ITEMS", "itemIds 最多选择三项。")
        if requested_ids:
            placeholders = ",".join("?" for _ in requested_ids)
            rows = conn.execute(
                f"SELECT id,status FROM cet_review_items WHERE id IN ({placeholders})", requested_ids
            ).fetchall()
            if len(rows) != len(set(requested_ids)) or any(row["status"] != "pending" for row in rows):
                _error(409, "REVIEW_ITEM_UNAVAILABLE", "部分卡点不存在或已完成。")
            order = {item_id: index for index, item_id in enumerate(requested_ids)}
            item_ids = sorted(set(requested_ids), key=order.get)
        else:
            rows = conn.execute(
                "SELECT id FROM cet_review_items WHERE status='pending' ORDER BY priority DESC,updated_at DESC,id LIMIT 12"
            ).fetchall()
            item_ids = [row["id"] for row in rows[:3]]
        if not item_ids:
            _error(409, "NO_REVIEW_ITEMS", "暂时没有待复习卡点。")
        now = _now()
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_reviews(id,status,budget_ms,current_position,created_at,updated_at) VALUES(?,'ready',?,0,?,?)",
            (review_id, budget, now, now),
        )
        if not inserted.rowcount:
            return _review_payload(review_id)
        for position, item_id in enumerate(item_ids):
            item = conn.execute("SELECT practice_id FROM cet_review_items WHERE id=?", (item_id,)).fetchone()
            conn.execute(
                "INSERT INTO cet_review_entries(id,review_id,position,item_id,practice_id,status) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, review_id, position, item_id, item["practice_id"], "current" if position == 0 else "pending"),
            )
    return _review_payload(review_id)


def _activate_entry(conn, review: Any, entry: Any, activate_practice: bool = True) -> None:
    conn.execute("UPDATE cet_review_entries SET status='current' WHERE id=?", (entry["id"],))
    conn.execute(
        "UPDATE cet_reviews SET current_position=?,updated_at=? WHERE id=?",
        (entry["position"], _now(), review["id"]),
    )
    if not activate_practice:
        return
    practice = _practice_row(conn, entry["practice_id"])
    if practice["status"] == "complete":
        conn.execute(
            "UPDATE cet_practices SET status='active',completed_at=NULL,completion_id=NULL,updated_at=? WHERE id=?",
            (_now(), practice["id"]),
        )
        practice = _practice_row(conn, practice["id"])
        _new_step(conn, practice, "replay", "review", show_text=False, payload={"reviewId": review["id"], "resumed": True})


def _move_to_next_entry(conn, review: Any, activate_practice: bool) -> None:
    next_entry = conn.execute(
        "SELECT * FROM cet_review_entries WHERE review_id=? AND status='pending' ORDER BY position LIMIT 1",
        (review["id"],),
    ).fetchone()
    if not next_entry:
        conn.execute("UPDATE cet_reviews SET status='complete',updated_at=? WHERE id=?", (_now(), review["id"]))
        return
    _activate_entry(conn, review, next_entry, activate_practice)


def review_action(review_id: str, body: dict[str, Any]) -> dict[str, Any]:
    action_id = str(body.get("actionId") or "").strip()
    action = str(body.get("action") or "").strip()
    action = {"begin": "start", "continue": "resume"}.get(action, action)
    if not action_id or action not in {"start", "pause", "resume", "skip", "replace", "complete", "finish"}:
        _error(422, "INVALID_REVIEW_ACTION", "复习操作或 actionId 无效。")
    with _db() as conn:
        duplicate = conn.execute(
            "SELECT id FROM cet_review_actions WHERE review_id=? AND id=?", (review_id, action_id)
        ).fetchone()
        if duplicate:
            return _review_payload(review_id)
        review = conn.execute("SELECT * FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
        if not review:
            _error(404, "REVIEW_NOT_FOUND", "复习会话不存在。", reviewId=review_id)
        entry = conn.execute(
            "SELECT * FROM cet_review_entries WHERE review_id=? AND position=?", (review_id, review["current_position"])
        ).fetchone()
        if body.get("currentEntryId") and (not entry or body["currentEntryId"] != entry["id"]):
            _error(409, "STALE_REVIEW_ENTRY", "当前复习条目已变化。")
        if action == "pause" and review["status"] == "paused":
            return _review_payload(review_id)
        if action == "resume" and review["status"] == "active":
            return _review_payload(review_id)
        if action == "finish" and review["status"] == "complete":
            return _review_payload(review_id)
        if review["status"] == "complete":
            _error(409, "REVIEW_COMPLETE", "复习会话已结束。")
        now = _now()
        if action == "start":
            if review["status"] != "ready":
                _error(409, "INVALID_REVIEW_STATE", "仅 ready 会话可以开始。")
            conn.execute("UPDATE cet_reviews SET status='active',revision=revision+1,updated_at=? WHERE id=?", (now, review_id))
            review = conn.execute("SELECT * FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
            _activate_entry(conn, review, entry)
        elif action == "pause":
            if review["status"] != "active":
                _error(409, "INVALID_REVIEW_STATE", "仅进行中的复习可以暂停。")
            if entry:
                practice = _practice_row(conn, entry["practice_id"])
                _cancel_pending_ai(conn, practice)
                conn.execute("UPDATE cet_practices SET revision=revision+1,updated_at=? WHERE id=?", (now, practice["id"]))
            conn.execute(
                "UPDATE cet_reviews SET status='paused',pause_count=pause_count+1,revision=revision+1,updated_at=? WHERE id=?",
                (now, review_id),
            )
        elif action == "resume":
            if review["status"] != "paused":
                _error(409, "INVALID_REVIEW_STATE", "仅暂停的复习可以继续。")
            if review["effective_ms"] >= review["budget_ms"]:
                conn.execute("UPDATE cet_reviews SET status='complete',updated_at=? WHERE id=?", (now, review_id))
            else:
                conn.execute("UPDATE cet_reviews SET status='active',revision=revision+1,updated_at=? WHERE id=?", (now, review_id))
                review = conn.execute("SELECT * FROM cet_reviews WHERE id=?", (review_id,)).fetchone()
                _activate_entry(conn, review, entry)
        elif action in {"skip", "complete"}:
            if review["status"] not in {"active", "paused"} or not entry:
                _error(409, "INVALID_REVIEW_STATE", "当前没有可操作的复习条目。")
            conn.execute(
                "UPDATE cet_review_entries SET status=? WHERE id=?",
                ("done" if action == "complete" else "skipped", entry["id"]),
            )
            if action == "complete":
                conn.execute("UPDATE cet_review_items SET status='done',updated_at=? WHERE id=?", (now, entry["item_id"]))
            # Skip never finishes the practice, creates feedback, or marks the global item done.
            _move_to_next_entry(conn, review, review["status"] == "active")
            conn.execute("UPDATE cet_reviews SET revision=revision+1,updated_at=? WHERE id=?", (now, review_id))
        elif action == "replace":
            if review["status"] not in {"active", "paused"} or not entry:
                _error(409, "INVALID_REVIEW_STATE", "当前没有可替换的复习条目。")
            replacement = conn.execute(
                "SELECT i.* FROM cet_review_items i WHERE i.status='pending' AND NOT EXISTS "
                "(SELECT 1 FROM cet_review_entries e WHERE e.review_id=? AND e.item_id=i.id) "
                "ORDER BY i.priority DESC,i.updated_at DESC,i.id LIMIT 1",
                (review_id,),
            ).fetchone()
            if not replacement:
                _error(409, "NO_REVIEW_REPLACEMENT", "没有其他待复习卡点。")
            conn.execute("UPDATE cet_review_entries SET status='skipped' WHERE id=?", (entry["id"],))
            position = conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 AS next FROM cet_review_entries WHERE review_id=?", (review_id,)
            ).fetchone()["next"]
            new_entry_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO cet_review_entries(id,review_id,position,item_id,practice_id,status) VALUES(?,?,?,?,?,'pending')",
                (new_entry_id, review_id, position, replacement["id"], replacement["practice_id"]),
            )
            new_entry = conn.execute("SELECT * FROM cet_review_entries WHERE id=?", (new_entry_id,)).fetchone()
            _activate_entry(conn, review, new_entry, review["status"] == "active")
            conn.execute("UPDATE cet_reviews SET revision=revision+1,updated_at=? WHERE id=?", (now, review_id))
        elif action == "finish":
            if entry:
                practice = _practice_row(conn, entry["practice_id"])
                _cancel_pending_ai(conn, practice)
                conn.execute("UPDATE cet_practices SET revision=revision+1,updated_at=? WHERE id=?", (now, practice["id"]))
            conn.execute("UPDATE cet_reviews SET status='complete',revision=revision+1,updated_at=? WHERE id=?", (now, review_id))
        inserted = conn.execute(
            "INSERT OR IGNORE INTO cet_review_actions(id,review_id,action,payload_json,created_at) VALUES(?,?,?,?,?)",
            (action_id, review_id, action, _json({"currentEntryId": body.get("currentEntryId")}), now),
        )
        if not inserted.rowcount:
            conn.rollback()
            return _review_payload(review_id)
    return _review_payload(review_id)
