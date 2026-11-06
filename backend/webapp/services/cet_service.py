"""CET listening black-box business services backed by EchoLingo's SQLite database."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
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


_ATTEMPT_IDEMPOTENCY_STATE = "unknown"


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
                # B6：review entries 加 replaced_from 列（可追溯替换来源）
                _ensure_review_entry_columns(conn)
                # N4/C01/C02：交卷幂等约束 + 兜底映射表
                _ensure_attempt_idem_map(conn)
                _ensure_attempt_idempotency(conn)
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


def _business_id(lesson: dict[str, Any]) -> str:
    """从课程的 source_url / title 派生稳定的业务 ID。

    例如 source_url = "file://cet6-202512-conv2.mp3"
    → 业务 ID "cet6-202512-conv2"
    这样同一份材料无论导入几次，对外 ID 都不变。
    取不到时返回空串（调用方保持底座 ID）。
    """
    src = str(lesson.get("source_url") or "")
    # 只有真实文件来源才派生业务 ID；
    # fixture:// 是测试夹具、upload: 是浏览器上传，都没有稳定业务含义，
    # 强制派生会让「同一份材料换台机器 ID 就变」，反而失去稳定性。
    if src.startswith("fixture://") or src.startswith("upload:"):
        return ""
    if src:
        name = src.rsplit("/", 1)[-1]          # cet6-202512-conv2.mp3
        name = name.rsplit(".", 1)[0]          # cet6-202512-conv2
        if name:
            return name
    return ""


def _lesson_id(material_id: str) -> int:
    """把外部传入的 materialId 解析成底座 lesson id。

    支持两种身份（任务书 7.1 要求「不用路径作为材料唯一身份」）：
      1. 底座原生形式 v2-<数字>            —— B 原有实现
      2. 业务稳定 ID，如 cet6-202512-conv2 —— 团队契约与前端使用的形式

    业务 ID 的解析方式：按 lesson 的 source_url / title 做**稳定匹配**
    （source_url 形如 file://cet6-202512-conv2.mp3，含业务 ID）。
    这样同一份材料无论导入几次，业务 ID 都不变，符合「稳定身份」的要求。
    """
    match = _MATERIAL_RE.fullmatch(material_id)
    if match:
        return int(match.group(1))

    # 业务 ID：按 source_url 或 title 后缀匹配第一个未归档课程
    with _db() as conn:
        row = conn.execute(
            "SELECT id, source_url, title FROM v2_lessons "
            "WHERE COALESCE(archived,0)=0 "
            "AND (source_url LIKE ? OR title LIKE ?) "
            "ORDER BY id DESC LIMIT 1",
            (f"%{material_id}%", f"%{material_id}%"),
        ).fetchone()
    if not row:
        _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", materialId=material_id)
    return int(row["id"])


_AUDIO_HASH_CACHE: dict[str, str] = {}
_AUDIO_HASH_LOCK = threading.Lock()


def _audio_sha256(lesson: dict[str, Any]) -> str:
    """材料的音频哈希（F10：用于核对「是不是同一段音频」）。

    优先级：
      1. 数据库里已存的哈希字段（最快）
      2. **实际读取音频文件计算 SHA-256**（F10 修复：原来只读字段，
         字段从没被写入过，所以恒为空串）
      3. 文件不可读时返回空串 —— 不编造值

    结果按「媒体 URL」缓存，避免每次列表请求都重算（105 秒音频约 1.6MB）。
    """
    for key in ("audio_sha256", "media_sha256", "audioSha256"):
        value = lesson.get(key)
        if value:
            return str(value)

    media_url = str(lesson.get("media_url") or "")
    if not media_url:
        return ""

    with _AUDIO_HASH_LOCK:
        cached = _AUDIO_HASH_CACHE.get(media_url)
    if cached is not None:
        return cached

    digest = ""
    try:
        # media_url 形如 /output/v2_assets/2181/xxx.mp3，映射到仓库根目录下的文件
        rel = media_url.lstrip("/")
        candidates = [
            Path(db.BASE_DIR) / rel if hasattr(db, "BASE_DIR") else None,
            Path.cwd() / rel,
        ]
        for path in candidates:
            if path and path.is_file():
                hasher = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
                break
    except Exception:  # noqa: BLE001
        digest = ""

    with _AUDIO_HASH_LOCK:
        _AUDIO_HASH_CACHE[media_url] = digest
    return digest


def _question_count(material_id: str, version: str) -> int:
    """该材料的题目数（契约要求）。

    先按精确版本查；查不到时回退到「该材料任意版本」。
    原因：材料版本是按 mediaUrl+segments 算的哈希，改一次 media_url
    （比如音频换个存放路径）版本就变，但题目并没有变。
    只按精确版本查会出现「列表显示 0 题、点进去却有题」的矛盾。
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM cet_questions "
                "WHERE material_id = ? AND material_version = ?",
                (material_id, version),
            ).fetchone()
            exact = int(row["c"] if row else 0)
            if exact:
                return exact
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM cet_questions WHERE material_id = ?",
                (material_id,),
            ).fetchone()
            return int(row["c"] if row else 0)
    except Exception:  # noqa: BLE001
        return 0


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
    start = float(segment["start"])
    end = float(segment["end"])
    return {
        "materialId": material_id,
        "materialVersion": version,
        "sentenceId": f"v2-{lesson_id}-{int(segment['index'])}",
        # 契约字段（startSec/endSec 是绝对秒；index 是句序）
        "index": int(segment["index"]),
        "startSec": start,
        "endSec": end,
        # 底座旧字段名，保留兼容
        "startSeconds": start,
        "endSeconds": end,
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
    # 字段名对齐团队契约（C 的 tests/fixtures/cet/ 与 docs/qa/接口规格.md）：
    #   id → materialId、version → materialVersion、durationSeconds → durationSec
    # 并补 developmentFixture / section / audioSha256 / questionCount，
    # 让前端与契约测试能按同一套字段工作。
    result = {
        # 契约字段
        "materialId": material_id,
        "materialVersion": version,
        # 旧字段别名：B 的自测与既有调用方仍读 id/version，保留以免回归
        "id": material_id,
        "version": version,
        "title": lesson.get("title") or f"听力材料 {lesson_id}",
        "level": _level(lesson),
        "section": "",
        "audioUrl": lesson.get("media_url") or "",
        # F09：前端/启动脚本据此拼出工作区地址，不硬编码 lesson id
        "lessonUrl": f"/workspace/{lesson_id}",
        "audioSha256": _audio_sha256(lesson),
        "durationSec": float(lesson.get("duration") or 0),
        "durationSeconds": float(lesson.get("duration") or 0),
        "source": {
            "type": lesson.get("source_type") or "",
            "value": lesson.get("source_url") or "",
            "materialStore": "echolingo.v2_lessons",
        },
        "sentenceCount": len(sentences),
        "questionCount": _question_count(material_id, version),
        "developmentFixture": bool(lesson.get("development_fixture")),
    }
    if include_sentences:
        result["sentences"] = sentences
    return result


def list_materials(level: str | None = None) -> list[dict[str, Any]]:
    if level is not None and level not in {"cet4", "cet6", "unspecified"}:
        _error(422, "INVALID_LEVEL", "level 仅支持 cet4、cet6 或 unspecified。")
    lessons = db.list_v2_lessons()
    materials = []
    for lesson in lessons:
        item = _material(lesson, include_sentences=False)
        # 列表也回显**业务稳定 ID**：底座 materialId 是 v2-<数字>，
        # 会随导入次数变化；业务 ID 由 source_url 派生，稳定可引用。
        # 任务书 7.1 要求「不用路径作为材料唯一身份」，这正是它的落点。
        biz = _business_id(lesson)
        if biz:
            item["materialId"] = biz
            item["id"] = biz
        materials.append(item)
    return [item for item in materials if level is None or item["level"] == level]


def _ensure_review_entry_columns(conn) -> None:
    """B6：给 cet_review_entries 补 replaced_from 列（记录替换来源，可追溯）。

    SQLite 的 ALTER TABLE ADD COLUMN 幂等：已存在会报错，用 try 包住。
    """
    try:
        conn.execute("ALTER TABLE cet_review_entries ADD COLUMN replaced_from TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        # 列已存在
        pass


def _ensure_attempt_idem_map(conn) -> None:
    """C02：幂等键映射表。

    唯一索引在「历史有重复」时建不上（needs_dedup），此时新会话仍会并发写多条。
    这里用一张独立的映射表做兜底：只要主键唯一，并发也只能有一条成功。
    历史记录一条都不动。
    """
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cet_attempt_idem ("
            "  idem_key TEXT PRIMARY KEY,"
            "  attempt_id TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _ensure_attempt_idempotency(conn) -> None:
    """给「同一材料 + 非空听音会话」加唯一约束，**不删除任何历史记录**。

    B02 教训：上一版在启动时无条件 DELETE 重复记录（保留最小 rowid），
    而 cet_attempt_answers 是 ON DELETE CASCADE —— 历史作答与答案会被一起删掉，
    且材料版本/答案内容都没比对，旧客户端保存的 attemptId 会失效。
    这里改为：
      1. 只对「session 非空且完全重复」的记录建立映射归档，不删除原记录
      2. 唯一索引建在**部分索引**上（仅 session 非空），空 session 不参与约束
      3. 失败时明确记录状态，不静默 pass
    """
    global _ATTEMPT_IDEMPOTENCY_STATE
    try:
        # C01：必须检查**已有索引的真实定义**。
        # CREATE ... IF NOT EXISTS 遇到同名旧索引（全量、无 WHERE）不会更新，
        # 却会把状态误报为 ok —— 上一版就踩了这个坑：
        # 旧全量索引继续生效，两个空 session 的作答被当成重复，返回同一 attemptId。
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='cet_attempts_session_uniq'"
        ).fetchone()
        existing_sql = (row["sql"] or "") if row else ""

        def _is_partial(sql: str) -> bool:
            norm = " ".join(sql.lower().split())
            return " where " in norm and "listening_session_id" in norm

        if existing_sql and not _is_partial(existing_sql):
            # 旧全量索引：先删再建（不删任何数据，只替换索引定义）
            conn.execute("DROP INDEX IF EXISTS cet_attempts_session_uniq")
            conn.commit()
            existing_sql = ""

        if not existing_sql:
            conn.execute(
                "CREATE UNIQUE INDEX cet_attempts_session_uniq "
                "ON cet_attempts(material_id, listening_session_id) "
                "WHERE listening_session_id IS NOT NULL AND listening_session_id <> ''"
            )
            conn.commit()

        # 复核：确认现在确实是部分索引
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='cet_attempts_session_uniq'"
        ).fetchone()
        if row and _is_partial(row["sql"] or ""):
            _ATTEMPT_IDEMPOTENCY_STATE = "ok"
        else:
            _ATTEMPT_IDEMPOTENCY_STATE = "failed: index definition not applied"
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        _ATTEMPT_IDEMPOTENCY_STATE = f"needs_dedup: {exc}"
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _ATTEMPT_IDEMPOTENCY_STATE = f"failed: {exc}"


def attempt_idempotency_state() -> str:
    """供诊断接口/日志查询：唯一索引是否就绪。"""
    return _ATTEMPT_IDEMPOTENCY_STATE


def find_material_by_lesson(lesson_id: int) -> dict[str, Any] | None:
    """按底座 lesson id 反查四六级材料。

    前端在 /workspace/<lesson_id> 页面里，最可靠的匹配依据就是 lesson id，
    不要用标题字符串模糊匹配 —— 实测会命中标题相近的其他课程。
    返回 None 表示这个课程不是四六级材料。
    """
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson or int(lesson.get("archived") or 0):
        return None
    # 只有打过 cet4/cet6 标签的课程才算四六级材料
    level = _level(lesson)
    if level not in {"cet4", "cet6"}:
        return None
    material = _material(lesson)
    biz = _business_id(lesson)
    if biz:
        material["materialId"] = biz
        material["id"] = biz
    return material


def get_material(material_id: str) -> dict[str, Any]:
    lesson = db.get_v2_lesson(_lesson_id(material_id))
    if not lesson or int(lesson.get("archived") or 0):
        _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", materialId=material_id)
    material = _material(lesson)
    # 调用方用业务 ID（如 cet6-202512-conv2）查询时，响应里回显同一个 ID，
    # 否则前端拿到的物化 ID 变了，后续所有请求都会对不上。
    if not _MATERIAL_RE.fullmatch(material_id):
        material["materialId"] = material_id
        material["id"] = material_id
    return material


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
    # 题目按**物化 ID**（v2-<数字>）存储；用业务 ID 查询时需换回物化 ID，
    # 否则查不到（B 的 replace_questions_for_fixture 用的是 v2-<id> 写入）。
    storage_id = f"v2-{_lesson_id(material_id)}"
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM cet_questions WHERE material_id=? AND material_version=? ORDER BY question_number",
            (storage_id, material["materialVersion"]),
        ).fetchall()
        if not rows:
            # 材料版本是按 mediaUrl+segments 算的哈希，改一次 mediaUrl 就变。
            # 题目若仍挂在旧版本下会查不到（表现为「题数突然变 0」）。
            # 这里回退到该材料的任意版本，并把版本号对齐到当前，
            # 避免因为元数据微调而丢失整批题目。
            rows = conn.execute(
                "SELECT * FROM cet_questions WHERE material_id=? ORDER BY question_number",
                (storage_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE cet_questions SET material_version=? WHERE material_id=?",
                    (material["materialVersion"], storage_id),
                )
                conn.commit()
    # Deliberately omit answers, sources and evidence before an attempt is submitted.
    # 字段对齐契约（questions_before_attempt.json）：
    #   id → questionId、questionNumber → number、prompt → stem
    #   options 从 [{id,text}] 扁平化为字符串数组（契约形式，A 按索引作答）
    #   补 materialId / materialVersion / answerSource
    # 交卷前**不返回** answer / evidenceRef / explanation（契约硬要求）。
    questions = [
        {
            "questionId": row["id"],
            "number": row["question_number"],
            "stem": row["prompt"],
            # 旧字段别名（B 自测读 id/questionNumber/prompt）
            "id": row["id"],
            "questionNumber": row["question_number"],
            "prompt": row["prompt"],
            "options": [opt["text"] for opt in _options(row["options_json"])],
            "materialId": material_id,
            "materialVersion": material["materialVersion"],
            # 交卷前**不返回** answerSource：它会暴露「该题有无可靠答案」，
            # 前端据此可间接推断，属信息泄露。B 原本的取舍更安全，予以采纳。
        }
        for row in rows
    ]
    return {"materialId": material_id, "materialVersion": material["materialVersion"], "questions": questions}


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
            if material["materialVersion"] == attempt["material_version"]:
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
    # 契约要求：交卷后 answersVisible=true，且每条结果能给 A 直接渲染的字段
    # （choice / answer / correct / answerSource / evidenceRef / explanation）。
    # B 原有 answers 保留（含更细的诊断字段），新增 results 与之并行，避免破坏既有调用。
    results = []
    for row in answers:
        row = dict(row)
        letter = str(row.get("answer") or "")
        correct_letter = row.get("correctAnswer")
        results.append({
            "questionId": row.get("questionId"),
            "number": row.get("questionNumber"),
            # 注意：未作答时 letter 是空串，而 Python 里 '' in 'ABCDE' 为 True，
            # 直接 .index('') 会得到 0（= A），把「没作答」显示成「选了 A」。
            # 必须先判空再判是否在字母表里。
            "choice": ("ABCDE".index(letter) if letter and letter in "ABCDE" else None),
            "answer": ("ABCDE".index(str(correct_letter))
                       if correct_letter and str(correct_letter) in "ABCDE" else None),
            "correct": row.get("isCorrect"),
            "answerSource": row.get("answerSource"),
            "evidenceRef": ({
                "sentenceId": row["evidence"]["sentenceId"],
                "startSec": row["evidence"]["startSeconds"],
                "endSec": row["evidence"]["endSeconds"],
                "quote": row["evidence"]["text"],
            } if row.get("evidence") else None),
            "explanation": "",
            "materialId": attempt["material_id"],
            "materialVersion": attempt["material_version"],
            "answerVisible": True,
        })
    # 未作答数动态算（不新增列，避免破坏已有数据库）
    unanswered_count = sum(
        1 for row in answers if not str(row.get("answer") or "").strip()
    )
    return {
        "attemptId": attempt["id"],
        "unansweredCount": unanswered_count,
        "materialId": attempt["material_id"],
        "materialVersion": attempt["material_version"],
        "listeningSessionId": attempt["listening_session_id"],
        "score": attempt["score"],
        "gradedCount": attempt["graded_count"],
        "unknownCount": attempt["unknown_count"],
        "answers": answers,
        "results": results,
        "answersVisible": True,
        "submittedAt": attempt["created_at"],
    }


def get_attempt(attempt_id: str) -> dict[str, Any]:
    """按 attemptId 取回交卷结果（F03：刷新/重开后要能恢复交卷状态）。"""
    return _attempt_payload(attempt_id)


def submit_attempt(body: dict[str, Any]) -> dict[str, Any]:
    # 契约：attemptId 由服务端生成；幂等靠 listeningSessionId（同一会话只交一次卷）。
    # 原实现要求客户端自带 attemptId，A 按契约调用会直接 422。
    material_id = str(body.get("materialId") or "").strip()
    session_id = str(body.get("listeningSessionId") or "").strip()
    attempt_id = str(body.get("attemptId") or "").strip()
    if not attempt_id:
        if not session_id:
            _error(422, "ATTEMPT_ID_REQUIRED", "materialId 和 listeningSessionId 不能为空。")
        # 同一听音会话复用同一 attempt（幂等）
        # C02：用幂等映射表做**原子占位**。
        # 唯一索引在历史有重复时建不上（needs_dedup），那时并发会各建一条。
        # 映射表主键唯一，并发时只有一个能插入成功，其余读回同一个 attemptId。
        # 历史记录一条都不动。
        idem_key = f"{material_id}::{session_id}"
        candidate_id = f"attempt-{uuid.uuid4().hex[:12]}"
        with _db() as _conn:
            prev = _conn.execute(
                "SELECT id FROM cet_attempts WHERE material_id=? AND listening_session_id=?",
                (material_id, session_id),
            ).fetchone()
            if prev:
                attempt_id = prev["id"]
            else:
                try:
                    _conn.execute(
                        "INSERT INTO cet_attempt_idem(idem_key,attempt_id,created_at) VALUES(?,?,?)",
                        (idem_key, candidate_id, _now()),
                    )
                    _conn.commit()
                    attempt_id = candidate_id
                except sqlite3.IntegrityError:
                    # 并发下别人先占位：读回它的 attemptId
                    _conn.rollback()
                    held = _conn.execute(
                        "SELECT attempt_id FROM cet_attempt_idem WHERE idem_key=?", (idem_key,)
                    ).fetchone()
                    attempt_id = held["attempt_id"] if held else candidate_id
    if not material_id:
        _error(422, "ATTEMPT_ID_REQUIRED", "materialId 不能为空。")
    with _db() as conn:
        existing = conn.execute("SELECT material_id FROM cet_attempts WHERE id=?", (attempt_id,)).fetchone()
    if existing:
        if existing["material_id"] != material_id:
            _error(409, "IDEMPOTENCY_CONFLICT", "attemptId 已用于另一份材料。")
        # D1：重复交卷必须**比对答案**。
        # 原实现直接返回旧结果，等于「静默接受改答案」——
        # 用户以为改成功了，实际还是旧答案，这是隐性数据错误。
        # 规则：答案完全相同 → 幂等返回；有变化 → 409 显式拒绝。
        submitted = {}
        for item in (body.get("answers") or []):
            if isinstance(item, dict) and item.get("questionId"):
                raw = item.get("choice")
                if raw is None:
                    raw = item.get("answer")
                if isinstance(raw, int) and not isinstance(raw, bool):
                    submitted[str(item["questionId"])] = "ABCDE"[raw] if 0 <= raw < 5 else ""
                else:
                    submitted[str(item["questionId"])] = str(raw or "").strip().upper()
        if submitted:
            with _db() as _conn:
                rows = _conn.execute(
                    "SELECT question_id, answer FROM cet_attempt_answers WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchall()
            stored = {r["question_id"]: str(r["answer"] or "").strip().upper() for r in rows}
            changed = [
                qid for qid, ans in submitted.items()
                if qid in stored and stored[qid] != ans
            ]
            if changed:
                _error(
                    409, "ATTEMPT_ANSWER_CONFLICT",
                    "交卷后不能修改答案。如需重做请使用新的听音会话。",
                    attemptId=attempt_id, changedQuestionIds=changed,
                )
        return _attempt_payload(attempt_id)
    material = get_material(material_id)
    if body.get("materialVersion") != material["materialVersion"]:
        _error(409, "MATERIAL_VERSION_CHANGED", "材料版本已变化，请刷新后重试。", currentVersion=material["materialVersion"])
    question_data = list_questions(material_id)["questions"]
    questions = {item["questionId"]: item for item in question_data}
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
        # 契约字段 choice 优先（0-based 选项索引或字母均可），兼容旧的 answer
        raw_choice = item.get("choice")
        if raw_choice is None:
            raw_choice = item.get("answer")
        if isinstance(raw_choice, int) and not isinstance(raw_choice, bool):
            answers[question_id] = "ABCDE"[raw_choice] if 0 <= raw_choice < 5 else ""
        else:
            answers[question_id] = str(raw_choice or "").strip().upper()
    with _db() as conn:
        # 题目按物化 ID（v2-<数字>）存储，且版本可能因元数据微调而落后；
        # 这里与 list_questions 用同一套解析，避免「列表有题、交卷判 0 条」。
        storage_id = f"v2-{_lesson_id(material_id)}"
        rows = conn.execute(
            "SELECT * FROM cet_questions WHERE material_id=? AND material_version=? ORDER BY question_number",
            (storage_id, material["materialVersion"]),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM cet_questions WHERE material_id=? ORDER BY question_number",
                (storage_id,),
            ).fetchall()
        score = graded = unknown = unanswered = 0
        answer_rows = []
        for row in rows:
            answer = answers.get(row["id"], "")
            correct = row["correct_answer"]
            if correct is None or not str(correct).strip():
                status, is_correct = "unknown", None
                unknown += 1
            elif not answer:
                # 未作答：既不判对也不判错（不能把空白当错答，也不能当对）
                status, is_correct = "unanswered", None
                unanswered += 1
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
            (attempt_id, material_id, material["materialVersion"], str(body.get("listeningSessionId") or ""), score, graded, unknown, now),
        )
        if not inserted.rowcount:
            # N4：唯一索引拦下了重复（并发下另一请求已写入）。
            # 按 attemptId 查不到时，按「材料 + 会话」回查真实记录并返回它，
            # 保证并发交卷只有一个 attemptId。
            existing = conn.execute(
                "SELECT id, material_id FROM cet_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if existing:
                if existing["material_id"] != material_id:
                    _error(409, "IDEMPOTENCY_CONFLICT", "attemptId 已用于另一份材料。")
                return _attempt_payload(existing["id"])
            same_session = conn.execute(
                "SELECT id FROM cet_attempts WHERE material_id=? AND listening_session_id=?",
                (material_id, str(body.get("listeningSessionId") or "")),
            ).fetchone()
            if same_session:
                return _attempt_payload(same_session["id"])
            _error(409, "IDEMPOTENCY_CONFLICT", "交卷记录冲突，请重试。")
        conn.executemany(
            "INSERT INTO cet_attempt_answers(attempt_id,question_id,answer,correct_answer,status,is_correct,answer_source,evidence_sentence_id) "
            "VALUES(?,?,?,?,?,?,?,?)", answer_rows,
        )
    return _attempt_payload(attempt_id)


def _mark_payload(row: Any) -> dict[str, Any]:
    # 字段对齐契约：
    #   id → markId、rawSeconds → pressedAtSec（契约要求保留「原始按键秒数」这一语义）
    # adjustedSeconds / speed / reactionDelaySeconds 仍保留（B 自己的诊断信息，契约不禁止）
    return {
        "markId": row["id"],
        "listeningSessionId": row["listening_session_id"],
        "materialId": row["material_id"],
        "materialVersion": row["material_version"],
        "pressedAtSec": row["raw_seconds"],
        # 旧字段别名（B 自测读 rawSeconds / id）
        "rawSeconds": row["raw_seconds"],
        "id": row["id"],
        "adjustedSeconds": row["adjusted_seconds"],
        "speed": row["speed"],
        "reactionDelaySeconds": row["reaction_delay_seconds"],
        "suggestedSentenceId": row["suggested_sentence_id"],
        "suggestionSource": "estimated_alignment",
        "confirmedSentenceId": row["confirmed_sentence_id"],
        # 表里没有独立 confirmed_at 列；确认动作会刷新 updated_at，用它表示确认时间
        "confirmedAtSec": row["updated_at"] if row["confirmed_sentence_id"] else None,
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
    # 契约要求：markId 由**服务端生成**（客户端只传材料、会话与按键秒数），
    # 且原始按键字段名为 pressedAtSec（兼容旧的 rawSeconds）。
    # 原实现要求客户端自带 markId 且只认 rawSeconds，A 按契约调用会直接 422。
    mark_id = str(body.get("markId") or body.get("id") or "").strip()
    if not mark_id:
        mark_id = f"mark-{uuid.uuid4().hex[:12]}"
    material_id = str(body.get("materialId") or "").strip()
    session_id = str(body.get("listeningSessionId") or "").strip()
    if not material_id or not session_id:
        _error(422, "MARK_ID_REQUIRED", "materialId 和 listeningSessionId 不能为空。")
    with _db() as conn:
        existing = conn.execute("SELECT * FROM cet_marks WHERE id=?", (mark_id,)).fetchone()
    if existing:
        if existing["material_id"] != material_id:
            _error(409, "IDEMPOTENCY_CONFLICT", "markId 已用于另一份材料。")
        return _mark_payload(existing)
    try:
        # 契约字段 pressedAtSec 优先；rawSeconds 保留兼容
        raw = body.get("pressedAtSec")
        if raw is None:
            raw = body.get("rawSeconds")
        seconds = float(raw)
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
    if body.get("materialVersion") != material["materialVersion"]:
        _error(409, "MATERIAL_VERSION_CHANGED", "材料版本已变化，请刷新后重试。", currentVersion=material["materialVersion"])
    if material["durationSec"] > 0 and seconds > material["durationSec"] + 0.001:
        _error(422, "INVALID_AUDIO_TIME", "按键时间超出原音频范围。")
    adjusted = max(0.0, seconds - reaction_delay * speed)
    if material["durationSec"] > 0:
        adjusted = min(adjusted, material["durationSec"])
    suggested_id = str(body.get("suggestedSentenceId") or "").strip()
    suggested = _find_sentence(material, suggested_id) if suggested_id else _suggest_sentence(material, adjusted)
    now = _now()
    with _db() as conn:
        nearby = conn.execute(
            "SELECT * FROM cet_marks WHERE listening_session_id=? AND material_id=? AND material_version=? "
            "AND ABS(raw_seconds-?)<=1.5 ORDER BY ABS(raw_seconds-?),created_at LIMIT 1",
            (session_id, material_id, material["materialVersion"], seconds, seconds),
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
            (mark_id, session_id, material_id, material["materialVersion"], seconds, adjusted, speed, reaction_delay,
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
        if row["material_version"] != material["materialVersion"]:
            _error(409, "MATERIAL_VERSION_CHANGED", "标记所属材料版本已变化。", currentVersion=material["materialVersion"])
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
                    str(question["id"]), material_id, material["materialVersion"], int(question["questionNumber"]),
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
    # B2：playback 同时给契约键与旧键（冻结约定 §3 双键返回）
    play_range = None if row["start_seconds"] is None else {
        "startSec": row["start_seconds"], "endSec": row["end_seconds"],
        "startSeconds": row["start_seconds"], "endSeconds": row["end_seconds"],
    }
    show_text = bool(row["show_text"])
    return {
        # B03：契约要求 stepId / playback / textVisible；底座旧字段 id / playRange / showText 保留
        "stepId": row["id"],
        "id": row["id"],
        "sequence": row["sequence"],
        "action": row["action"],
        "source": row["source"],
        "playRange": play_range,
        "playback": ({**play_range, "speed": row["speed"]} if play_range
                     else {"speed": row["speed"]}),
        "speed": row["speed"],
        "showText": show_text,
        "textVisible": show_text,
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
        # 字段对齐契约：id → practiceId、sentence → sentenceId
        # （保留原 id/sentence 供 B 既有调用方使用，属新增而非替换）
        "practiceId": practice["id"], "id": practice["id"],
        "markId": practice["mark_id"],
        "materialId": practice["material_id"], "materialVersion": practice["material_version"],
        "sentenceId": (sentence or {}).get("sentenceId") if isinstance(sentence, dict) else sentence,
        "sentence": sentence, "audioUrl": practice["audio_url"],
        "status": practice["status"], "revision": practice["revision"],
        "currentStepId": practice["current_step_id"],
        "currentStep": _step_payload(current_step) if current_step else None,
        # B03：契约要求 step（当前步骤）与 stepCount
        "step": _step_payload(current_step) if current_step else None,
        "stepCount": len(steps),
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
    # 契约：practiceId 由服务端生成（客户端只传 markId）。兼容客户端自带。
    mark_id = str(body.get("markId") or "").strip()
    practice_id = str(body.get("practiceId") or "").strip() or f"practice-{uuid.uuid4().hex[:12]}"
    if not mark_id:
        _error(422, "PRACTICE_ID_REQUIRED", "markId 不能为空。")
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
    if material["materialVersion"] != mark["material_version"]:
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


def _local_practice_action_conn(
    conn, practice_id: str, body: dict[str, Any], *, already_validated: bool = False
) -> bool:
    """在**调用方给定的连接**内执行本地动作。

    S2：拆出连接内版本，让「校验 + 反馈 + 步骤推进」能在同一事务完成。
    返回 True 表示已处理；False 表示幂等重复（调用方直接返回当前状态）。

    参数 already_validated=True 时跳过 stepId/version 校验
    （调用方已在同一事务里校验过）。
    """
    action = str(body.get("action") or "").strip()
    action_id = str(body.get("actionId") or "").strip()
    if action not in {"replay", "slow", "chunk", "explain"} or not action_id:
        _error(422, "INVALID_LOCAL_ACTION", "本地动作仅支持 replay、slow、chunk、explain，且需要 actionId。")
    duplicate = conn.execute(
        "SELECT id FROM cet_practice_events WHERE practice_id=? AND id=?", (practice_id, action_id)
    ).fetchone()
    if duplicate:
        return False
    practice = _practice_row(conn, practice_id)
    if practice["status"] != "active":
        _error(409, "PRACTICE_COMPLETE", "练习已结束。")
    if not already_validated:
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "步骤或材料版本已变化，请刷新后重试。")
    # C03 语义区分：被新动作/新请求取代的旧 AI 请求标 stale，
    # 只有用户主动取消才标 cancelled（两者终态都可查）。
    _cancel_pending_ai(conn, practice, status="stale")
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
        (action_id, practice_id, practice["current_step_id"], practice["material_version"],
         _json({"action": action}), _now()),
    )
    return True


def local_practice_action(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """独立入口（供路由直接调用）：自己开连接、自己提交。"""
    with _db() as conn:
        _local_practice_action_conn(conn, practice_id, body)
        conn.commit()
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
    material_version = _material(lesson, include_sentences=False)["materialVersion"]
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


def _action_for_feedback(feedback: str) -> str:
    """任务书 7.3：sound（没听清）→ 慢放；meaning（不懂意思）→ 解释；
    heard（自评听懂）→ 换句检查。这三条是契约规定的语义。"""
    return {"sound": "slow", "meaning": "explain", "heard": "transfer"}.get(feedback, "replay")


def _persist_feedback_conn(conn, practice: Any, feedback: str, request_id: str) -> None:
    """在**已校验**的连接内记录反馈（不 commit，由调用方事务控制）。

    R2：校验失败时整个事务回滚，被拒绝的请求不会污染 latestFeedback。
    """
    event_id = f"fb-{request_id}"
    if conn.execute("SELECT id FROM cet_practice_events WHERE id=?", (event_id,)).fetchone():
        return
    conn.execute(
        "INSERT INTO cet_practice_events"
        "(id,practice_id,step_id,material_version,event_type,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            event_id, practice["id"], practice["current_step_id"],
            practice["material_version"], "feedback",
            _json({"feedback": feedback, "source": "user"}), _now(),
        ),
    )
    # 注意：这里**不能** revision+1。
    # 后面的 _new_step / local_practice_action 用 revision 做乐观锁，
    # 提前自增会让它们拿到旧 revision 而误判 STALE_RESPONSE（实测复现）。
    # 反馈落库本身通过事件表保证幂等，不需要改 revision。
    conn.execute(
        "UPDATE cet_practices SET latest_feedback=?, feedback_source='user', "
        "updated_at=? WHERE id=?",
        (feedback, _now(), practice["id"]),
    )


def _persist_feedback(practice_id: str, feedback: str, request_id: str) -> None:
    """把用户反馈写入练习记录与事件表（幂等）。

    F04 的根因：原实现只用 feedback 选动作，不落库，
    导致 latest_feedback 恒为 null、feedbackHistory 为空，
    AI 请求读不到反馈，训练记录里也没有用户判断。
    """
    with _db() as conn:
        practice = conn.execute(
            "SELECT id, current_step_id FROM cet_practices WHERE id=?", (practice_id,)
        ).fetchone()
        if not practice:
            _error(404, "PRACTICE_NOT_FOUND", "练习不存在。", practiceId=practice_id)
        event_id = f"fb-{request_id}"
        duplicate = conn.execute(
            "SELECT id FROM cet_practice_events WHERE id=?", (event_id,)
        ).fetchone()
        if duplicate:
            return
        conn.execute(
            "INSERT INTO cet_practice_events"
            "(id,practice_id,step_id,material_version,event_type,payload_json,created_at) "
            "SELECT ?,?,?,?,?,?,?",
            (
                event_id, practice_id, practice["current_step_id"],
                conn.execute("SELECT material_version FROM cet_practices WHERE id=?",
                             (practice_id,)).fetchone()["material_version"],
                "feedback",
                _json({"feedback": feedback, "source": "user"}),
                _now(),
            ),
        )
        conn.execute(
            "UPDATE cet_practices SET latest_feedback=?, feedback_source='user', "
            "revision=revision+1, updated_at=? WHERE id=?",
            (feedback, _now(), practice_id),
        )
        conn.commit()


def next_practice_step(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    # 契约用 feedback（sound/meaning/heard）驱动下一步，由服务端决定动作；
    # B 原实现要求客户端传 action + requestId。两者都支持：
    #   有 feedback  → 按契约语义由服务端选动作
    #   只有 action  → 保持 B 原有行为（兼容）
    request_id = str(body.get("requestId") or "").strip() or f"req-{uuid.uuid4().hex[:12]}"
    feedback = str(body.get("feedback") or "").strip()
    if feedback:
        if feedback not in {"sound", "meaning", "heard"}:
            _error(422, "INVALID_FEEDBACK", "feedback 仅支持 sound、meaning、heard。")
        requested = _action_for_feedback(feedback)
    else:
        requested = str(body.get("action") or "").strip()
    if requested and requested not in {"replay", "slow", "chunk", "explain", "transfer"}:
        _error(422, "INVALID_ACTION", "不支持的动作。")

    # R2：反馈落库必须在**校验通过之后**、且与状态推进同一事务。
    # 原实现先调 _persist_feedback（内部独立 commit），再校验 stepId/version，
    # 导致 409 被拒绝的请求仍然改写了 latestFeedback 与 feedbackHistory。
    with _db() as conn:
        previous = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
        if previous:
            if previous["status"] == "succeeded":
                return _practice_payload(practice_id)
            _error(409 if previous["status"] == "pending" else 502, "AI_REQUEST_NOT_RETRYABLE", "该请求已处理，请使用新的 requestId 重试。")
        # N1：必须**先取写锁再校验**。
        # 原实现在首次 SELECT 后没有取得写事务，两个并发请求都能通过校验；
        # 第二个请求等锁后拿到新状态，却因 already_validated 跳过复查而被接受。
        # 这里用一条写语句强制升级到 RESERVED 锁，后续 SELECT 看到的就是
        # 「取得锁之后」的状态，两个同旧步骤请求最多一个能推进。
        conn.execute(
            "UPDATE cet_practices SET updated_at=updated_at WHERE id=?", (practice_id,)
        )
        practice = _practice_row(conn, practice_id)
        if practice["status"] != "active":
            _error(409, "PRACTICE_COMPLETE", "练习已结束。")
        if body.get("stepId") != practice["current_step_id"] or body.get("materialVersion") != practice["material_version"]:
            _error(409, "STALE_RESPONSE", "步骤或材料版本已变化，请刷新后重试。")
        # 校验全部通过后才记录反馈（同一事务，失败一起回滚）
        if feedback:
            _persist_feedback_conn(conn, practice, feedback, request_id)
            # S1：practice 是更新前的快照，必须重新读取，
            # 否则后面 request_data 与 generate() 会拿到旧的 latest_feedback
            #（实测：meaning 入库了，模型却收到 sound）。
            practice = conn.execute(
                "SELECT * FROM cet_practices WHERE id=?", (practice_id,)
            ).fetchone()
        action = _next_action(conn, practice, requested)
        # S2：本地动作在**同一事务**内完成（校验 + 反馈 + 步骤推进），
        # 不再提前 commit 回避锁冲突 —— 提前 commit 会破坏原子性：
        # 并发交错时被 409 拒绝的请求仍留下本次反馈。
        if action in {"replay", "slow"}:
            # N1：不跳过校验。已持写锁，这里复查的是「锁后状态」，
            # 并发交错时能正确拒绝过期请求。
            handled = _local_practice_action_conn(
                conn, practice_id,
                {**body, "actionId": request_id, "action": action},
                already_validated=False,
            )
            conn.commit()
            return _practice_payload(practice_id)
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
    # S1：模型参数必须与本次反馈一致。
    # 优先用本次请求的 feedback；没有（如 replay）才回退到库里最新值。
    model_feedback = feedback or (practice["latest_feedback"] or "")
    try:
        if action == "transfer" and candidate is None:
            generated, model = {"noCandidate": True, "reason": "no_candidate"}, ""
        else:
            from webapp.services import cet_ai
            generated, model = cet_ai.generate(action, sentence, model_feedback, candidate)
    except cet_ai.CETAIError as exc:
        with _db() as conn:
            current = _practice_row(conn, practice_id)
            if current["ai_request_id"] == request_id and current["revision"] == revision:
                conn.execute(
                    "UPDATE cet_ai_requests SET status='failed',error_code=?,completed_at=? WHERE id=? AND status='pending'",
                    (exc.code, _now(), request_id),
                )
                # B4：失败后**保留 ai_request_id 指针**，让终态可查
                #（ai-status 能返回 failed 及 errorCode）。
                # 但请求状态已是 failed，_cancel_pending_ai 只处理 pending，
                # 所以不会被误当成「还有请求在跑」：
                #   - 后续事件上报不会因此被判 STALE_RESPONSE
                #   - 有效用时不会因 ai_waiting 一直不累计（只看 pending）
                conn.execute(
                    "UPDATE cet_practices SET revision=?, updated_at=? "
                    "WHERE id=? AND revision=?",
                    (revision, _now(), practice_id, revision),
                )
                conn.commit()
            else:
                conn.execute("UPDATE cet_ai_requests SET status='stale',completed_at=? "
                         "WHERE id=? AND status <> 'cancelled'",
                         (_now(), request_id))
                conn.commit()
                _error(409, "STALE_RESPONSE", "模型错误结果已过期，未改变当前练习。", practiceId=practice_id, requestId=request_id)
        # 503 也要带回当前练习状态：反馈已经落库了，
        # 前端需要知道 latestFeedback / 当前步骤，才能继续操作而不是白屏。
        try:
            state = _practice_payload(practice_id)
        except Exception:  # noqa: BLE001
            state = {}
        _error(exc.status, exc.code, exc.message, practiceId=practice_id,
               requestId=request_id, practice=state)
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        request = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
        if (
            practice["status"] != "active" or practice["revision"] != revision
            or practice["current_step_id"] != request["step_id"] or practice["ai_request_id"] != request_id
            or request["status"] != "pending"
        ):
            conn.execute("UPDATE cet_ai_requests SET status='stale',completed_at=? "
                         "WHERE id=? AND status <> 'cancelled'",
                         (_now(), request_id))
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


def ai_request_status(practice_id: str) -> dict[str, Any]:
    """B03：查询当前 AI 请求状态（契约要求可查询）。

    返回 aiRequest：{requestId, status, action, createdAt} 或 None。
    status 枚举：pending / succeeded / failed / cancelled / stale / idle。
    """
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        request_id = practice["ai_request_id"]
        if not request_id:
            return {"practiceId": practice_id, "aiRequest": None, "status": "idle"}
        row = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return {"practiceId": practice_id, "aiRequest": None, "status": "idle"}
        return {
            "practiceId": practice_id,
            "status": row["status"],
            "aiRequest": {
                "requestId": row["id"],
                "status": row["status"],
                "action": row["action"],
                "errorCode": row["error_code"],
                "createdAt": row["created_at"],
                "completedAt": row["completed_at"],
            },
        }


def cancel_ai_request(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """C03：取消指定 AI 请求。

    契约：body.requestId 指定要取消的请求（**必须匹配当前 active 请求**）。
    - 重试取消 A 不会影响后来启动的 B（目标身份参与判断）
    - 取消后保留请求记录与终态，不把 aiRequest 置空
    - 迟到响应只能被拒绝，不能把 cancelled 覆盖成 stale
    """
    target_id = str((body or {}).get("requestId") or "").strip()
    with _db() as conn:
        # 先取写锁，避免与完成/失败路径竞争
        conn.execute("UPDATE cet_practices SET updated_at=updated_at WHERE id=?", (practice_id,))
        practice = _practice_row(conn, practice_id)
        active_id = practice["ai_request_id"]

        # 未指定目标：只取消当前 active 请求
        if not target_id:
            target_id = active_id or ""

        if not target_id:
            conn.commit()
            return {"practiceId": practice_id, "aiRequest": None, "status": "idle",
                    "cancelled": False, "reason": "no_active_request"}

        row = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (target_id,)).fetchone()
        if not row or row["practice_id"] != practice_id:
            conn.commit()
            return {"practiceId": practice_id, "status": "not_found",
                    "cancelled": False, "reason": "request_not_found",
                    "aiRequest": None}

        # 目标不是当前 active 请求：说明它已被处理过，不能误伤新请求
        if active_id and target_id != active_id:
            conn.commit()
            state = _ai_request_payload(conn, row)
            return {"practiceId": practice_id, "aiRequest": state,
                    "status": row["status"], "cancelled": False,
                    "reason": "not_the_active_request"}

        if row["status"] == "pending":
            conn.execute(
                "UPDATE cet_ai_requests SET status='cancelled',completed_at=? "
                "WHERE id=? AND status='pending'",
                (_now(), target_id),
            )
            # C03：保留 ai_request_id 指针（终态可查），
            # 但请求状态已是 cancelled，不再被当 active（_cancel_pending_ai 只看 pending）。
            conn.execute(
                "UPDATE cet_practices SET updated_at=? WHERE id=?",
                (_now(), practice_id),
            )
            cancelled = True
        else:
            cancelled = False
        conn.commit()
        fresh = conn.execute("SELECT * FROM cet_ai_requests WHERE id=?", (target_id,)).fetchone()
        return {
            "practiceId": practice_id,
            "aiRequest": _ai_request_payload(conn, fresh),
            "status": fresh["status"],
            "cancelled": cancelled,
        }


def _ai_request_payload(conn, row) -> dict[str, Any] | None:
    """C03：请求终态快照（取消后仍可查）。"""
    if not row:
        return None
    return {
        "requestId": row["id"],
        "status": row["status"],
        "action": row["action"],
        "errorCode": row["error_code"],
        "createdAt": row["created_at"],
        "completedAt": row["completed_at"],
    }


def record_practice_event(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    event_id = str(body.get("eventId") or "").strip()
    event_type = str(body.get("type") or body.get("eventType") or "").strip()
    allowed = {"playback_start", "playback_end", "playback_interrupt",
               "activity_heartbeat", "visibility", "feedback", "transfer_answer"}
    # B1：短名作为别名接受（冻结约定 §1）。
    # 早期 Mock 与部分客户端用 start/end/interrupt，真实后端必须兼容，
    # 否则同一份契约在两边表现不一致。
    _EVENT_SHORT_ALIASES = {
        "start": "playback_start",
        "end": "playback_end",
        "interrupt": "playback_interrupt",
    }
    event_type = _EVENT_SHORT_ALIASES.get(event_type, event_type)
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
        # C04：在**事务内**提交 effective_ms 累加，
        # 再让 _practice_payload 读到新值（原实现读到累加前的旧值）。
        conn.commit()
    result = _practice_payload(practice_id)
    result["event"] = {"eventId": event_id, "duplicate": False, "appliedMs": applied_ms, **payload}
    # B03/C04：契约要求顶层 effectiveMs 与 status
    result.setdefault("effectiveMs", 0)
    result.setdefault("status", "complete" if result.get("practiceStatus") == "complete" else "active")
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


def _finish_payload(practice: dict[str, Any], review_item: Any) -> dict[str, Any]:
    """B3：finish 响应摊平。

    原来只返回 {practice, reviewItem}，调用方（前端/测试）习惯读顶层
    status / latestFeedback / effectiveMs，导致拿不到。
    这里把 practice 的字段同时放到顶层，practice 子对象保留兼容。
    """
    return {**practice, "practice": practice, "reviewItem": review_item}


def finish_practice(practice_id: str, body: dict[str, Any]) -> dict[str, Any]:
    finish_id = str(body.get("finishId") or "").strip()
    if not finish_id:
        finish_id = f"finish-{uuid.uuid4().hex[:12]}"
    with _db() as conn:
        practice = _practice_row(conn, practice_id)
        if practice["status"] == "complete":
            item = conn.execute(
                "SELECT i.*,p.sentence_json,p.audio_url FROM cet_review_items i JOIN cet_practices p ON p.id=i.practice_id "
                "WHERE i.material_id=? AND i.material_version=? AND i.sentence_id=?",
                (practice["material_id"], practice["material_version"], practice["sentence_id"]),
            ).fetchone()
            return _finish_payload(_practice_payload(practice_id), _review_item_payload(item))
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
    return _finish_payload(_practice_payload(practice_id), item_payload)


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
            # B6：排除被 replace 替换掉的旧条目（status='skipped' 且已被同位置新条目取代），
            # 否则替换后条目数会从 3 变 4。
            # 注意：用户主动 skip 的条目仍要显示（它也是 skipped），
            # 所以用「同位置是否存在其他条目」来区分。
            "SELECT e.id AS entry_id,e.position,e.status AS entry_status,i.*,p.sentence_json,p.audio_url "
            "FROM cet_review_entries e JOIN cet_review_items i ON i.id=e.item_id "
            "JOIN cet_practices p ON p.id=e.practice_id WHERE e.review_id=? "
            "AND NOT (e.status='skipped' AND EXISTS ("
            "  SELECT 1 FROM cet_review_entries e2 "
            "  WHERE e2.review_id=e.review_id AND e2.position=e.position AND e2.id<>e.id"
            ")) ORDER BY e.position",
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
            # B03：契约字段别名
            "entryId": row["entry_id"], "itemId": row["id"],
            # B7：对外 order 用 1-based（「第 1 处」），position 仍是 0-based 内部索引
            "order": row["position"] + 1,
        })
    result = {
        "id": review["id"], "status": review["status"], "budgetMs": review["budget_ms"],
        "effectiveMs": review["effective_ms"], "remainingMs": max(0, review["budget_ms"] - review["effective_ms"]),
        "currentPosition": review["current_position"], "revision": review["revision"],
        "pauseCount": review["pause_count"], "entries": entries,
        # B03：契约要求 reviewId / items；底座旧字段 id / entries 保留兼容
        "reviewId": review["id"],
        "items": entries,
        "count": len(entries),
        "currentEntryId": next((entry["id"] for entry in entries if entry["position"] == review["current_position"]), None)
        if review["status"] != "complete" else None,
        "createdAt": review["created_at"], "updatedAt": review["updated_at"],
    }
    if include_practice:
        result["currentPractice"] = _practice_payload(current_practice_id) if current_practice_id else None
    return result


def create_review(body: dict[str, Any]) -> dict[str, Any]:
    # 契约：reviewId 由服务端生成。兼容客户端自带。
    review_id = str(body.get("reviewId") or "").strip() or f"review-{uuid.uuid4().hex[:12]}"
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
            # B6：替换应**就地更新原条目**，不是追加、也不是同位置插入。
            # 原实现在 MAX+1 处插新条目 → 条目数 3 变 4（不是「替换」）。
            # 改成同位置插入会撞 UNIQUE(review_id, position)，所以直接 UPDATE。
            # 旧 item 的身份通过 replacedFrom 留痕，可追溯。
            new_entry_id = entry["id"]
            conn.execute(
                "UPDATE cet_review_entries SET item_id=?,practice_id=?,status='pending',"
                "replaced_from=? WHERE id=?",
                (replacement["id"], replacement["practice_id"],
                 entry["item_id"], entry["id"]),
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
