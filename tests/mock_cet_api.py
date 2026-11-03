#!/usr/bin/env python3
"""四六级听力黑匣子 · 开发专用 Mock API（C 负责）。

这是给前端 A 在本机开发时用的**可运行替身**，不是真实后端。
它的目的是让 A 在 B 的业务接口就绪前，就能按第 7 节契约走通页面与交互。

边界（写在代码里，避免误用）：
- 这里的返回值一律来自 `tests/fixtures/cet/` 下的开发样例，带 developmentFixture 标识。
- Mock 通过只证明「契约与测试工具可运行」，**不证明真实业务正确**。
- 真实后端的验收必须用同一套断言打真实地址，见 tests/test_cet_contract.py。

启动：
    .venv/bin/python tests/mock_cet_api.py                 # 默认 127.0.0.1:5199
    CET_MOCK_PORT=5199 .venv/bin/python tests/mock_cet_api.py
    CET_MOCK_SCENARIO=slow .venv/bin/python tests/mock_cet_api.py

场景切换（三种方式，优先级从高到低）：
    1. 单次请求查询参数            ?scenario=empty
    2. 单次请求请求头              X-CET-Scenario: slow
    3. 服务启动环境变量            CET_MOCK_SCENARIO=error
支持场景：normal（默认）｜empty（空列表）｜slow（慢响应）｜error（故障）

前端切换方式：把 API base 指到 http://127.0.0.1:5199 即可；切回真实后端只改 base。

会话内状态：
    标记、作答、练习、复习会话都保存在进程内存，一次 Mock 会话内可连续往下走。
    `POST /api/cet/_mock/reset` 清空；`GET /api/cet/_mock/state` 查看。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "cet"

SCENARIOS = ("normal", "empty", "slow", "error")
SLOW_DELAY_SEC = float(os.environ.get("CET_MOCK_SLOW_SEC", "2.5"))

# 本地音频路径：音频不入库，通过环境变量或默认本地目录映射。
AUDIO_CANDIDATES = [
    os.environ.get("CET_QA_MEDIA", ""),
    str(HERE.parent / ".cet-qa" / "media" / "cet6-202512-conv2.mp3"),
]


# ────────────────────────── 工具 ──────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def load_fixture(name: str) -> Any:
    path = FIXTURES / name
    if not path.exists():
        raise SystemExit(
            f"[mock] 缺少契约样例 {path}；请先运行 "
            f"`.venv/bin/python tests/fixtures/cet/build_fixtures.py`"
        )
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def error_body(code: str, message: str, action: str = "") -> dict:
    """与底座 webapp.constants.ERROR_CATALOG 的 error_info 形状保持一致。"""
    return {
        "error": message,
        "error_info": {
            "code": code,
            "title": message,
            "message": message,
            "action": action or message,
            "step": None,
            "detail": None,
            "developmentFixture": True,
        },
    }


def resolve_scenario(
    request: Request,
    scenario: Optional[str],
    scenario_header: Optional[str],
) -> str:
    chosen = scenario or scenario_header or os.environ.get("CET_MOCK_SCENARIO", "normal")
    chosen = (chosen or "normal").strip().lower()
    return chosen if chosen in SCENARIOS else "normal"


class ScenarioFailure(Exception):
    """场景注入用：由中间件统一转成错误响应。"""

    def __init__(self, code: str, status: int, message: str, action: str = ""):
        self.code, self.status, self.message, self.action = code, status, message, action


# ────────────────────────── 状态 ──────────────────────────


def fresh_state() -> dict:
    material_doc = load_fixture("material_cet6-202512-conv2.json")
    materials_doc = load_fixture("materials.json")
    before_doc = load_fixture("questions_before_attempt.json")
    after_doc = load_fixture("attempt_result.json")
    return {
        "materials": materials_doc["items"],
        "materialDetail": material_doc,
        "sentences": material_doc["sentences"],
        "questionsBefore": before_doc["items"],
        "questionsAfter": after_doc["items"],
        "attempts": {},          # attemptId -> record
        "attemptBySession": {},  # listeningSessionId -> attemptId
        "marks": {},             # markId -> record
        "markEvents": {},        # eventId -> markId（长按/连发去重）
        "practices": {},         # practiceId -> record
        "practiceEvents": {},    # eventId -> event record（重复上报去重）
        "reviews": {},           # reviewId -> record
        "requestSeq": 0,
    }


STATE = fresh_state()


def get_material(material_id: str) -> Optional[dict]:
    for item in STATE["materials"]:
        if item["materialId"] == material_id:
            return item
    return None


def sentences_for(material_id: str) -> list[dict]:
    return [s for s in STATE["sentences"] if s["materialId"] == material_id]


def find_sentence(material_id: str, sentence_id: str) -> Optional[dict]:
    for s in STATE["sentences"]:
        if s["materialId"] == material_id and s["sentenceId"] == sentence_id:
            return s
    return None


def snap_to_sentence(material_id: str, pressed_sec: float) -> Optional[dict]:
    """把原始按键秒数吸附到包含它的句子；不在任何句子内则取最近句子。"""
    sents = sentences_for(material_id)
    if not sents:
        return None
    for s in sents:
        if s["startSec"] <= pressed_sec <= s["endSec"]:
            return s
    return min(
        sents,
        key=lambda s: min(abs(s["startSec"] - pressed_sec), abs(s["endSec"] - pressed_sec)),
    )


# ────────────────────────── 应用 ──────────────────────────

app = FastAPI(
    title="CET Blackbox Mock API (development fixture)",
    version="cet-mock-v1",
    description=(
        "开发专用模拟接口。所有响应带 developmentFixture=true。"
        "Mock 通过只证明契约可运行，不证明真实后端正确。"
    ),
    docs_url="/api/cet/_mock/docs",
    openapi_url="/api/cet/_mock/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def scenario_middleware(request: Request, call_next):
    """场景注入集中在一处，避免每个路由重复写延迟/故障分支。

    只作用于 /api/cet/ 业务路径；`_mock/` 控制面始终按 normal 处理，
    否则一旦切到 error 场景就再也切不回来。
    """
    path = request.url.path
    if not path.startswith("/api/cet/") or path.startswith("/api/cet/_mock/"):
        return await call_next(request)

    scenario = resolve_scenario(
        request,
        request.query_params.get("scenario"),
        request.headers.get("x-cet-scenario"),
    )
    if scenario == "slow":
        await asyncio.sleep(SLOW_DELAY_SEC)
    if scenario == "error":
        body = error_body(
            "UPSTREAM_UNAVAILABLE",
            "模拟故障：上游服务不可用（scenario=error）",
            "这是开发样例注入的故障，用于验证页面错误态与重试；不是真实服务错误。",
        )
        body["error_info"]["scenario"] = "error"
        body["developmentFixture"] = True
        return JSONResponse(body, status_code=503)
    return await call_next(request)


@app.exception_handler(ScenarioFailure)
async def scenario_failure_handler(_request: Request, exc: ScenarioFailure):
    body = error_body(exc.code, exc.message, exc.action)
    return JSONResponse(body, status_code=exc.status)


def _scenario(request: Request) -> str:
    return resolve_scenario(
        request,
        request.query_params.get("scenario"),
        request.headers.get("x-cet-scenario"),
    )


def _check_version(material_id: str, material_version: str | None) -> None:
    """材料版本不一致时明确报错，避免页面拿旧版本数据覆盖新版本。"""
    mat = get_material(material_id)
    if mat is None:
        raise ScenarioFailure("MATERIAL_NOT_FOUND", 404, f"材料不存在：{material_id}")
    if material_version and material_version != mat["materialVersion"]:
        raise ScenarioFailure(
            "MATERIAL_VERSION_MISMATCH",
            409,
            f"材料版本不匹配：请求 {material_version}，当前 {mat['materialVersion']}",
            "请重新加载材料后再操作。",
        )


# ────────────────────────── 控制面 ──────────────────────────


@app.post("/api/cet/_mock/reset", tags=["mock"])
def mock_reset():
    global STATE
    STATE = fresh_state()
    return {"ok": True, "developmentFixture": True, "resetAt": now_iso()}


@app.get("/api/cet/_mock/state", tags=["mock"])
def mock_state():
    return {
        "developmentFixture": True,
        "scenarioEnv": os.environ.get("CET_MOCK_SCENARIO", "normal"),
        "counts": {
            "materials": len(STATE["materials"]),
            "sentences": len(STATE["sentences"]),
            "questions": len(STATE["questionsBefore"]),
            "attempts": len(STATE["attempts"]),
            "marks": len(STATE["marks"]),
            "practices": len(STATE["practices"]),
            "reviews": len(STATE["reviews"]),
        },
        "scenarios": list(SCENARIOS),
    }


@app.get("/api/cet/_mock/info", tags=["mock"])
def mock_info():
    return {
        "developmentFixture": True,
        "contractVersion": "cet-contract-v1",
        "slowDelaySec": SLOW_DELAY_SEC,
        "scenarios": {
            "normal": "正常返回",
            "empty": "材料列表返回空数组（验证空态）",
            "slow": f"每个业务请求延迟 {SLOW_DELAY_SEC}s（验证等待与超时）",
            "error": "每个业务请求返回 503 与 error_info（验证错误态与重试）",
        },
        "switchBy": [
            "query: ?scenario=slow",
            "header: X-CET-Scenario: empty",
            "env: CET_MOCK_SCENARIO=error（启动时）",
        ],
        "notice": "Mock 通过仅证明契约与测试工具可运行，真实业务需用同一套断言打真实服务。",
    }


# ────────────────────────── 材料与句子 ──────────────────────────


@app.get("/api/cet/materials", tags=["materials"])
def list_materials(request: Request):
    if _scenario(request) == "empty":
        return {"items": [], "count": 0, "developmentFixture": True}
    return {
        "items": STATE["materials"],
        "count": len(STATE["materials"]),
        "developmentFixture": True,
    }


@app.get("/api/cet/materials/{material_id}", tags=["materials"])
def get_material_detail(material_id: str, version: Optional[str] = None):
    _check_version(material_id, version)
    detail = STATE["materialDetail"]
    return {
        "material": get_material(material_id),
        "sentences": sentences_for(material_id),
        "provenance": detail.get("provenance", {}),
        "developmentFixture": True,
    }


@app.get("/api/cet/materials/{material_id}/audio", tags=["materials"])
def get_material_audio(material_id: str):
    """返回本地音频。音频不入库，靠本地路径映射；找不到时明确报错而不是静默 404。"""
    mat = get_material(material_id)
    if mat is None:
        raise ScenarioFailure("MATERIAL_NOT_FOUND", 404, f"材料不存在：{material_id}")
    for cand in AUDIO_CANDIDATES:
        if cand and Path(cand).is_file():
            return FileResponse(cand, media_type="audio/mpeg")
    raise ScenarioFailure(
        "MATERIAL_AUDIO_MISSING",
        404,
        "本地未找到样例音频",
        "请把 cet6-202512-conv2.mp3 放到 .cet-qa/media/，或设置 CET_QA_MEDIA 指向该文件。",
    )


# ────────────────────────── 题目与交卷 ──────────────────────────


@app.get("/api/cet/materials/{material_id}/questions", tags=["questions"])
def get_questions(material_id: str, version: Optional[str] = None):
    """交卷前视图：不含 answer / evidenceRef / explanation。"""
    _check_version(material_id, version)
    items = [q for q in STATE["questionsBefore"] if q["materialId"] == material_id]
    return {"items": items, "count": len(items), "answersVisible": False, "developmentFixture": True}


@app.post("/api/cet/attempts", tags=["questions"])
def submit_attempt(payload: dict = Body(...)):
    """交卷。返回参考答案与证据；同一听音会话重复提交保持幂等。"""
    material_id = str(payload.get("materialId") or "")
    material_version = payload.get("materialVersion")
    session_id = str(payload.get("listeningSessionId") or "")
    answers = payload.get("answers")
    if not material_id:
        raise ScenarioFailure("URL_REQUIRED", 400, "materialId 必填")
    _check_version(material_id, material_version)
    if not session_id:
        raise ScenarioFailure("URL_REQUIRED", 400, "listeningSessionId 必填")
    if not isinstance(answers, list) or not answers:
        raise ScenarioFailure("INVALID_ANSWERS", 400, "answers 必须是非空数组")

    normalized = []
    for item in answers:
        qid = str(item.get("questionId") or "")
        choice = item.get("choice")
        if not qid:
            raise ScenarioFailure("INVALID_ANSWERS", 400, "answers[*].questionId 必填")
        if choice is not None and (not isinstance(choice, int) or isinstance(choice, bool)):
            raise ScenarioFailure("INVALID_ANSWERS", 400, f"{qid} 的 choice 必须是整数或 null")
        normalized.append({"questionId": qid, "choice": choice})
    normalized.sort(key=lambda a: a["questionId"])

    existing_id = STATE["attemptBySession"].get(session_id)
    if existing_id:
        existing = STATE["attempts"][existing_id]
        if existing["answers"] == normalized:
            # 重复提交：返回同一 attemptId，不新增记录
            return {**existing["response"], "idempotent": True, "developmentFixture": True}
        raise ScenarioFailure(
            "ATTEMPT_ALREADY_SUBMITTED",
            409,
            "该听音会话已交卷，原始作答保留",
            "如需重做，请新建听音会话。",
        )

    after_by_id = {q["questionId"]: q for q in STATE["questionsAfter"]}
    results, correct_count, unknown_count, answered = [], 0, 0, 0
    for ans in normalized:
        q = after_by_id.get(ans["questionId"])
        if q is None:
            raise ScenarioFailure(
                "QUESTION_NOT_FOUND", 404, f"题目不存在：{ans['questionId']}"
            )
        answer = q.get("answer")
        choice = ans["choice"]
        if choice is not None:
            answered += 1
        if answer is None or q.get("answerSource") == "unknown":
            unknown_count += 1
            correct = None  # 未知答案：不判对错，保留 unknown
        else:
            correct = None if choice is None else choice == answer
            if correct:
                correct_count += 1
        results.append(
            {
                "questionId": q["questionId"],
                "number": q["number"],
                "stem": q["stem"],
                "options": q["options"],
                "choice": choice,
                "answer": answer,
                "answerSource": q.get("answerSource"),
                "correct": correct,
                "evidenceRef": q.get("evidenceRef"),
                "explanation": q.get("explanation", ""),
            }
        )

    attempt_id = new_id("attempt")
    response = {
        "attemptId": attempt_id,
        "materialId": material_id,
        "materialVersion": material_version,
        "listeningSessionId": session_id,
        "submittedAt": now_iso(),
        "answersVisible": True,
        "results": results,
        "summary": {
            "total": len(results),
            "answered": answered,
            "correct": correct_count,
            "unknownAnswer": unknown_count,
        },
        "developmentFixture": True,
    }
    STATE["attempts"][attempt_id] = {
        "attemptId": attempt_id,
        "answers": normalized,
        "response": response,
    }
    STATE["attemptBySession"][session_id] = attempt_id
    return response


# ────────────────────────── 标记与确认 ──────────────────────────


@app.post("/api/cet/marks", tags=["marks"])
def create_mark(payload: dict = Body(...)):
    material_id = str(payload.get("materialId") or "")
    material_version = payload.get("materialVersion")
    session_id = str(payload.get("listeningSessionId") or "")
    pressed = payload.get("pressedAtSec")
    event_id = payload.get("eventId")

    if not material_id or not session_id:
        raise ScenarioFailure("URL_REQUIRED", 400, "materialId 与 listeningSessionId 必填")
    _check_version(material_id, material_version)
    if not isinstance(pressed, (int, float)) or isinstance(pressed, bool):
        raise ScenarioFailure("INVALID_MARK", 400, "pressedAtSec 必须是数字（原音频绝对秒数）")
    mat = get_material(material_id)
    if pressed < 0 or pressed > float(mat["durationSec"]) + 0.001:
        raise ScenarioFailure(
            "MARK_OUT_OF_RANGE",
            422,
            f"按键时间 {pressed}s 超出材料时长 {mat['durationSec']}s",
        )

    # 重复上报（长按连发 / 网络重试）用 eventId 去重，不新增标记
    if event_id:
        if event_id in STATE["markEvents"]:
            mark_id = STATE["markEvents"][event_id]
            return {**STATE["marks"][mark_id], "idempotent": True, "developmentFixture": True}
    else:
        # 未带 eventId 时按「同一会话 + 同一秒内」去重，模拟长按不连发
        for m in STATE["marks"].values():
            if (
                m["listeningSessionId"] == session_id
                and abs(float(m["pressedAtSec"]) - float(pressed)) < 0.25
            ):
                return {**m, "idempotent": True, "developmentFixture": True}

    sug = snap_to_sentence(material_id, float(pressed))
    mark_id = new_id("mark")
    record = {
        "markId": mark_id,
        "listeningSessionId": session_id,
        "materialId": material_id,
        "materialVersion": mat["materialVersion"],
        "pressedAtSec": round(float(pressed), 3),
        "suggestedSentenceId": sug["sentenceId"] if sug else None,
        "confirmedSentenceId": None,
        "confirmedAtSec": None,
        "createdAt": now_iso(),
    }
    STATE["marks"][mark_id] = record
    if event_id:
        STATE["markEvents"][event_id] = mark_id
    return {**record, "developmentFixture": True}


@app.patch("/api/cet/marks/{mark_id}", tags=["marks"])
def update_mark(mark_id: str, payload: dict = Body(...)):
    """确认或调整句子。非法句子范围必须报具体句号，不能静默接受。"""
    mark = STATE["marks"].get(mark_id)
    if mark is None:
        raise ScenarioFailure("MARK_NOT_FOUND", 404, f"标记不存在：{mark_id}")
    confirmed = payload.get("confirmedSentenceId")
    if confirmed is None:
        raise ScenarioFailure("URL_REQUIRED", 400, "confirmedSentenceId 必填")
    sentence = find_sentence(mark["materialId"], str(confirmed))
    if sentence is None:
        raise ScenarioFailure(
            "SENTENCE_NOT_FOUND", 422, f"句子不存在：{confirmed}（材料 {mark['materialId']}）"
        )
    mark["confirmedSentenceId"] = sentence["sentenceId"]
    mark["confirmedAtSec"] = now_iso()
    return {**mark, "developmentFixture": True}


@app.get("/api/cet/marks", tags=["marks"])
def list_marks(material_id: str = Query(..., alias="materialId")):
    """按材料隔离查询；不返回其它材料或其它会话的标记。"""
    if get_material(material_id) is None:
        raise ScenarioFailure("MATERIAL_NOT_FOUND", 404, f"材料不存在：{material_id}")
    items = [m for m in STATE["marks"].values() if m["materialId"] == material_id]
    items.sort(key=lambda m: m["pressedAtSec"])
    return {
        "items": items,
        "count": len(items),
        "materialId": material_id,
        "developmentFixture": True,
    }


# ────────────────────────── 练习 ──────────────────────────


def _first_step(practice: dict, sentence: dict) -> dict:
    return {
        "stepId": new_id("step"),
        "practiceId": practice["practiceId"],
        "action": "replay",
        "source": "confirmed_sentence",
        "playback": {
            "startSec": sentence["startSec"],
            "endSec": sentence["endSec"],
            "speed": 1.0,
        },
        "textVisible": False,
        "feedback": None,
        "candidateRef": None,
        "createdAt": now_iso(),
    }


@app.post("/api/cet/practices", tags=["practices"])
def create_practice(payload: dict = Body(...)):
    mark_id = str(payload.get("markId") or "")
    mark = STATE["marks"].get(mark_id)
    if mark is None:
        raise ScenarioFailure("MARK_NOT_FOUND", 404, f"标记不存在：{mark_id}")
    if not mark.get("confirmedSentenceId"):
        raise ScenarioFailure(
            "MARK_NOT_CONFIRMED",
            409,
            "标记尚未确认句子",
            "请先在回听页确认卡住的句子，再进入带练。",
        )
    # 同一标记重复创建练习返回已有练习，避免重复启动产生重复记录
    for p in STATE["practices"].values():
        if p["markId"] == mark_id:
            return {**p, "idempotent": True, "developmentFixture": True}

    sentence = find_sentence(mark["materialId"], mark["confirmedSentenceId"])
    practice_id = new_id("practice")
    practice = {
        "practiceId": practice_id,
        "markId": mark_id,
        "materialId": mark["materialId"],
        "materialVersion": mark["materialVersion"],
        "sentenceId": sentence["sentenceId"],
        "status": "active",
        "currentStepId": None,
        "feedbackHistory": [],
        "effectiveMs": 0,
        "aiRequest": {"status": "idle", "requestId": None, "errorCode": None},
        "steps": [],
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    step = _first_step(practice, sentence)
    practice["currentStepId"] = step["stepId"]
    practice["steps"].append(step)
    STATE["practices"][practice_id] = practice
    return {
        **{k: v for k, v in practice.items() if k != "steps"},
        "currentStep": step,
        "stepCount": 1,
        "developmentFixture": True,
    }


@app.get("/api/cet/practices/{practice_id}", tags=["practices"])
def get_practice(practice_id: str):
    """服务重启/刷新页面后按原 practiceId 恢复同一练习。"""
    practice = STATE["practices"].get(practice_id)
    if practice is None:
        raise ScenarioFailure("PRACTICE_NOT_FOUND", 404, f"练习不存在：{practice_id}")
    current = next(
        (s for s in practice["steps"] if s["stepId"] == practice["currentStepId"]), None
    )
    return {
        **{k: v for k, v in practice.items() if k != "steps"},
        "currentStep": current,
        "stepCount": len(practice["steps"]),
        "developmentFixture": True,
    }


@app.post("/api/cet/practices/{practice_id}/events", tags=["practices"])
def practice_event(practice_id: str, payload: dict = Body(...)):
    """播放与反馈事件。按 eventId 幂等；只有前台有效播放才累计 effectiveMs。"""
    practice = STATE["practices"].get(practice_id)
    if practice is None:
        raise ScenarioFailure("PRACTICE_NOT_FOUND", 404, f"练习不存在：{practice_id}")

    event_id = str(payload.get("eventId") or "")
    if not event_id:
        raise ScenarioFailure("URL_REQUIRED", 400, "eventId 必填")
    if event_id in STATE["practiceEvents"]:
        return {
            "ok": True,
            "idempotent": True,
            "eventId": event_id,
            "effectiveMs": practice["effectiveMs"],
            "developmentFixture": True,
        }

    etype = str(payload.get("type") or "")
    if etype not in ("start", "end", "interrupt"):
        raise ScenarioFailure(
            "INVALID_EVENT", 422, f"事件类型无效：{etype}（应为 start / end / interrupt）"
        )
    material_version = payload.get("materialVersion")
    if material_version and material_version != practice["materialVersion"]:
        raise ScenarioFailure(
            "MATERIAL_VERSION_MISMATCH",
            409,
            f"事件材料版本 {material_version} 与练习 {practice['materialVersion']} 不一致",
        )
    step_id = payload.get("stepId")
    played_ms = payload.get("playedMs", 0)
    if played_ms is not None and (
        not isinstance(played_ms, (int, float)) or isinstance(played_ms, bool) or played_ms < 0
    ):
        raise ScenarioFailure("INVALID_EVENT", 422, "playedMs 必须是非负数字")
    visible = payload.get("visible", True)

    # 只有「前台 + 可见 + 有播放量」才计入有效学习时长；
    # AI 等待、后台、暂停由前端不打 end 事件或 visible=false 表示。
    counted = 0
    if etype in ("end", "interrupt") and visible and played_ms:
        counted = int(played_ms)
        practice["effectiveMs"] += counted

    STATE["practiceEvents"][event_id] = {
        "eventId": event_id,
        "practiceId": practice_id,
        "stepId": step_id,
        "type": etype,
        "playedMs": played_ms,
        "countedMs": counted,
        "visible": visible,
        "audioSec": payload.get("audioSec"),
        "speed": payload.get("speed"),
        "materialVersion": material_version,
        "receivedAt": now_iso(),
    }
    practice["updatedAt"] = now_iso()
    return {
        "ok": True,
        "idempotent": False,
        "eventId": event_id,
        "countedMs": counted,
        "effectiveMs": practice["effectiveMs"],
        "developmentFixture": True,
    }


def _next_action(feedback: str) -> str:
    """反馈 → 帮助动作。sound（没听清）先慢放，meaning（不懂意思）先解释。"""
    return {"sound": "slow", "meaning": "explain", "heard": "transfer"}.get(feedback, "replay")


@app.post("/api/cet/practices/{practice_id}/next", tags=["practices"])
def practice_next(practice_id: str, payload: dict = Body(...)):
    """AI 动作。请求绑定 practiceId + stepId + 版本；过期结果一律拒绝。"""
    practice = STATE["practices"].get(practice_id)
    if practice is None:
        raise ScenarioFailure("PRACTICE_NOT_FOUND", 404, f"练习不存在：{practice_id}")
    if practice["status"] != "active":
        raise ScenarioFailure(
            "PRACTICE_NOT_ACTIVE", 409, f"练习状态为 {practice['status']}，不能继续下一步"
        )

    feedback = str(payload.get("feedback") or "")
    if feedback not in ("sound", "meaning", "heard"):
        raise ScenarioFailure(
            "INVALID_FEEDBACK", 422, f"反馈值无效：{feedback}（应为 sound / meaning / heard）"
        )
    base_step_id = payload.get("stepId")
    if base_step_id and base_step_id != practice["currentStepId"]:
        # 迟到的模型结果：用户已切到别的步骤，旧结果必须失效
        raise ScenarioFailure(
            "STALE_STEP",
            409,
            f"请求基于旧步骤 {base_step_id}，当前步骤已是 {practice['currentStepId']}",
            "请按当前步骤重新请求；旧结果不得覆盖界面。",
        )

    sentence = find_sentence(practice["materialId"], practice["sentenceId"])
    action = _next_action(feedback)
    STATE["requestSeq"] += 1
    request_id = f"req-{STATE['requestSeq']:04d}"

    playback, text_visible, payload_extra = None, False, {}
    if action == "slow":
        playback = {"startSec": sentence["startSec"], "endSec": sentence["endSec"], "speed": 0.75}
    elif action == "explain":
        text_visible = True
        payload_extra = {
            "explanation": (
                f"（开发样例）原句：{sentence['text']} "
                "这句话的难点在弱读与连读；先看句子结构，再隐藏文字原速回听。"
            )
        }
    elif action == "transfer":
        playback = {"startSec": sentence["startSec"], "endSec": sentence["endSec"], "speed": 1.0}
        text_visible = False
        payload_extra = {"note": "（开发样例）隐藏原文、原速回听"}
    else:
        playback = {"startSec": sentence["startSec"], "endSec": sentence["endSec"], "speed": 1.0}

    step = {
        "stepId": new_id("step"),
        "practiceId": practice_id,
        "action": action,
        "source": "model",
        "modelRequestId": request_id,
        "playback": playback,
        "textVisible": text_visible,
        "feedback": feedback,
        "candidateRef": None,
        "payload": payload_extra,
        "createdAt": now_iso(),
    }
    practice["steps"].append(step)
    practice["currentStepId"] = step["stepId"]
    practice["feedbackHistory"].append(feedback)
    practice["aiRequest"] = {"status": "complete", "requestId": request_id, "errorCode": None}
    practice["updatedAt"] = now_iso()
    return {
        "practiceId": practice_id,
        "step": step,
        "modelRequest": {"requestId": request_id, "status": "complete"},
        "developmentFixture": True,
        "modelOutputIsFixture": True,
    }


@app.post("/api/cet/practices/{practice_id}/local", tags=["practices"])
def practice_local(practice_id: str, payload: dict = Body(...)):
    """本地动作：模型不可用时仍必须能慢放、短语回听、看原文并保存。"""
    practice = STATE["practices"].get(practice_id)
    if practice is None:
        raise ScenarioFailure("PRACTICE_NOT_FOUND", 404, f"练习不存在：{practice_id}")
    action = str(payload.get("action") or "")
    if action not in ("replay", "slow", "chunk", "explain"):
        raise ScenarioFailure(
            "INVALID_ACTION", 422, f"本地动作无效：{action}（应为 replay / slow / chunk / explain）"
        )
    sentence = find_sentence(practice["materialId"], practice["sentenceId"])
    text = sentence["text"]
    if action == "chunk":
        words = text.split()
        half = max(1, len(words) // 2)
        chunks = [" ".join(words[:half]), " ".join(words[half:])] if len(words) > 1 else [text]
        playback = {
            "startSec": sentence["startSec"],
            "endSec": sentence["endSec"],
            "speed": 0.85,
            "chunks": chunks,
        }
    else:
        playback = {
            "startSec": sentence["startSec"],
            "endSec": sentence["endSec"],
            "speed": {"slow": 0.75, "replay": 1.0, "explain": 1.0}.get(action, 1.0),
        }
    step = {
        "stepId": new_id("step"),
        "practiceId": practice_id,
        "action": action,
        "source": "local",
        "playback": playback,
        "textVisible": action == "explain",
        "feedback": None,
        "candidateRef": None,
        "payload": {"text": text} if action == "explain" else {},
        "createdAt": now_iso(),
    }
    practice["steps"].append(step)
    practice["currentStepId"] = step["stepId"]
    practice["updatedAt"] = now_iso()
    return {
        "practiceId": practice_id,
        "step": step,
        "modelUsed": False,
        "developmentFixture": True,
    }


@app.post("/api/cet/practices/{practice_id}/finish", tags=["practices"])
def practice_finish(practice_id: str, payload: dict = Body(default={})):
    """结束练习并保存卡点。重复结束保持幂等，不新增记录。"""
    practice = STATE["practices"].get(practice_id)
    if practice is None:
        raise ScenarioFailure("PRACTICE_NOT_FOUND", 404, f"练习不存在：{practice_id}")
    feedback = payload.get("feedback")
    if feedback is not None and feedback not in ("sound", "meaning", "heard"):
        raise ScenarioFailure("INVALID_FEEDBACK", 422, f"反馈值无效：{feedback}")
    if practice["status"] == "complete":
        return {
            "practiceId": practice_id,
            "status": "complete",
            "idempotent": True,
            "latestFeedback": practice.get("latestFeedback"),
            "developmentFixture": True,
        }
    if feedback:
        practice["feedbackHistory"].append(feedback)
        # 最新反馈覆盖早先结果：早先 heard 之后又 sound，最终应为 sound
        practice["latestFeedback"] = feedback
    practice["status"] = "complete"
    practice["completedAt"] = now_iso()
    practice["updatedAt"] = now_iso()
    return {
        "practiceId": practice_id,
        "status": "complete",
        "idempotent": False,
        "latestFeedback": practice.get("latestFeedback"),
        "effectiveMs": practice["effectiveMs"],
        "reviewItem": {
            "practiceId": practice_id,
            "markId": practice["markId"],
            "sentenceId": practice["sentenceId"],
            "materialId": practice["materialId"],
            "materialVersion": practice["materialVersion"],
            "latestFeedback": practice.get("latestFeedback"),
            "status": "pending",
        },
        "developmentFixture": True,
    }


# ────────────────────────── 卡点与复习 ──────────────────────────


@app.get("/api/cet/review-items", tags=["reviews"])
def review_items(material_id: Optional[str] = Query(None, alias="materialId")):
    """待复习卡点：以最新反馈为准，早先听懂不覆盖最新没听清。"""
    items = []
    for p in STATE["practices"].values():
        if p["status"] != "complete":
            continue
        if material_id and p["materialId"] != material_id:
            continue
        sentence = find_sentence(p["materialId"], p["sentenceId"])
        items.append(
            {
                "practiceId": p["practiceId"],
                "markId": p["markId"],
                "sentenceId": p["sentenceId"],
                "materialId": p["materialId"],
                "materialVersion": p["materialVersion"],
                "startSec": sentence["startSec"],
                "endSec": sentence["endSec"],
                "latestFeedback": p.get("latestFeedback"),
                "status": "pending",
                "effectiveMs": p["effectiveMs"],
            }
        )
    items.sort(key=lambda x: x["practiceId"])
    return {"items": items, "count": len(items), "developmentFixture": True}


@app.post("/api/cet/reviews", tags=["reviews"])
def create_review(payload: dict = Body(default={})):
    """创建十分钟复习。最多三处，按待练顺序排。"""
    max_items = payload.get("maxItems", 3)
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 10:
        raise ScenarioFailure("INVALID_BUDGET", 422, "maxItems 必须是 1—10 的整数")
    budget_ms = payload.get("budgetMs", 600_000)
    if not isinstance(budget_ms, int) or budget_ms <= 0:
        raise ScenarioFailure("INVALID_BUDGET", 422, "budgetMs 必须是正整数（毫秒）")

    pending = [
        p
        for p in STATE["practices"].values()
        if p["status"] == "complete" and p.get("latestFeedback") in ("sound", "meaning")
    ]
    pending.sort(key=lambda p: p.get("completedAt") or "")
    chosen = pending[:max_items]
    if not chosen:
        raise ScenarioFailure(
            "NO_REVIEW_ITEM",
            409,
            "没有可复习的卡点",
            "请先完成至少一处带练并保存。",
        )

    review_id = new_id("review")
    items = []
    for i, p in enumerate(chosen):
        sentence = find_sentence(p["materialId"], p["sentenceId"])
        items.append(
            {
                "itemId": f"item-{i + 1:03d}",
                "order": i + 1,
                "practiceId": p["practiceId"],  # 恢复时用原练习 ID
                "markId": p["markId"],
                "sentenceId": p["sentenceId"],
                "materialId": p["materialId"],
                "materialVersion": p["materialVersion"],
                "startSec": sentence["startSec"],
                "endSec": sentence["endSec"],
                "latestFeedback": p.get("latestFeedback"),
                "status": "pending",
            }
        )
    review = {
        "reviewId": review_id,
        "status": "ready",
        "budgetMs": budget_ms,
        "effectiveMs": 0,
        "maxItems": max_items,
        "currentItemId": None,
        "items": items,
        "createdAt": now_iso(),
    }
    STATE["reviews"][review_id] = review
    return {**review, "developmentFixture": True}


@app.get("/api/cet/reviews/{review_id}", tags=["reviews"])
def get_review(review_id: str):
    review = STATE["reviews"].get(review_id)
    if review is None:
        raise ScenarioFailure("REVIEW_NOT_FOUND", 404, f"复习会话不存在：{review_id}")
    return {**review, "developmentFixture": True}


@app.post("/api/cet/reviews/{review_id}/actions", tags=["reviews"])
def review_action(review_id: str, payload: dict = Body(...)):
    review = STATE["reviews"].get(review_id)
    if review is None:
        raise ScenarioFailure("REVIEW_NOT_FOUND", 404, f"复习会话不存在：{review_id}")
    action = str(payload.get("action") or "")
    item_id = payload.get("itemId")

    if action == "start":
        if review["status"] == "complete":
            raise ScenarioFailure("REVIEW_COMPLETE", 409, "复习会话已结束")
        review["status"] = "active"
        if review["currentItemId"] is None and review["items"]:
            review["currentItemId"] = review["items"][0]["itemId"]
            review["items"][0]["status"] = "current"
        return {**review, "developmentFixture": True}

    if action == "switch":
        target = next((i for i in review["items"] if i["itemId"] == item_id), None)
        if target is None:
            raise ScenarioFailure("ITEM_NOT_FOUND", 404, f"复习条目不存在：{item_id}")
        for i in review["items"]:
            if i["status"] == "current":
                i["status"] = "pending"
        target["status"] = "current"
        review["currentItemId"] = target["itemId"]
        review["status"] = "active"
        return {**review, "developmentFixture": True}

    if action == "done":
        target = next((i for i in review["items"] if i["itemId"] == item_id), None)
        if target is None:
            raise ScenarioFailure("ITEM_NOT_FOUND", 404, f"复习条目不存在：{item_id}")
        target["status"] = "done"
        nxt = next((i for i in review["items"] if i["status"] == "pending"), None)
        review["currentItemId"] = nxt["itemId"] if nxt else None
        if nxt:
            nxt["status"] = "current"
        else:
            review["status"] = "complete"
        return {**review, "developmentFixture": True}

    if action == "skip":
        target = next((i for i in review["items"] if i["itemId"] == item_id), None)
        if target is None:
            raise ScenarioFailure("ITEM_NOT_FOUND", 404, f"复习条目不存在：{item_id}")
        # 跳过的语义是「这次不听」，条目必须保留为待练，不能算听懂
        target["status"] = "skipped_pending"
        nxt = next(
            (i for i in review["items"] if i["status"] == "pending" and i["itemId"] != target["itemId"]),
            None,
        )
        review["currentItemId"] = nxt["itemId"] if nxt else None
        if nxt:
            nxt["status"] = "current"
        return {**review, "developmentFixture": True}

    if action == "pause":
        review["status"] = "paused"
        return {**review, "developmentFixture": True}

    if action == "resume":
        if review["status"] != "paused":
            raise ScenarioFailure("INVALID_STATE", 409, f"当前状态 {review['status']} 不能继续")
        review["status"] = "active"
        return {**review, "developmentFixture": True}

    if action == "tick":
        # 有效计时：只有 active 且前台才累计，暂停与 AI 等待不计
        delta = payload.get("elapsedMs", 0)
        if not isinstance(delta, int) or isinstance(delta, bool) or delta < 0:
            raise ScenarioFailure("INVALID_EVENT", 422, "elapsedMs 必须是非负整数（毫秒）")
        counted = 0
        if review["status"] == "active" and payload.get("visible", True):
            counted = min(delta, max(0, review["budgetMs"] - review["effectiveMs"]))
            review["effectiveMs"] += counted
        return {
            **review,
            "countedMs": counted,
            "budgetExhausted": review["effectiveMs"] >= review["budgetMs"],
            "developmentFixture": True,
        }

    raise ScenarioFailure(
        "INVALID_ACTION",
        422,
        f"复习动作无效：{action}（应为 start / switch / done / skip / pause / resume / tick）",
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("CET_MOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("CET_MOCK_PORT", "5199"))
    print(f"CET Mock API (development fixture) on http://{host}:{port}")
    print(f"  docs:     http://{host}:{port}/api/cet/_mock/docs")
    print(f"  scenario: {os.environ.get('CET_MOCK_SCENARIO', 'normal')} (normal|empty|slow|error)")
    print("  提醒：Mock 通过只证明契约可运行，真实业务需打真实服务验证。")
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
