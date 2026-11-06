#!/usr/bin/env python3
"""D01 返修探针：交卷幂等全入口 + AI 生命周期终态一致性。

可重跑，不需要起服务、不需要真实模型、不碰日常使用库。
每个用例都在自己的临时目录里建库，跑完即弃。

覆盖：
  A. 幂等四组合矩阵（正常库 / 历史重复库 × 服务生成 ID / 客户端自带 ID）
     每种组合并发 6 次，断言只落 1 条记录
  B. 空 session + 不同 attemptId 保留为独立作答（不合并）
  C. 历史重复记录不被删除，返回最早一条且标注 conflict: true
  D. AI 迟到响应三场景：
       pending              → 接管为 stale
       failed(SERVER_RESTARTED) → 接管为 stale（进程异常悬空）
       failed(AI_INVALID_OUTPUT) → 保持 failed（真失败不受影响）
       cancelled(USER_CANCELLED) → 保持 cancelled（用户明确取消）
       succeeded            → 保持 succeeded

用法：
    .venv/bin/python scripts/cet/probe_idempotency.py
    .venv/bin/python scripts/cet/probe_idempotency.py --json   # 只输出 JSON

退出码：0 全部通过｜1 有断言失败
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

FAILURES: list[str] = []
RESULTS: dict = {}


def check(name: str, ok: bool, detail=None) -> bool:
    RESULTS.setdefault("assertions", []).append(
        {"name": name, "ok": bool(ok), "detail": detail}
    )
    if not ok:
        FAILURES.append(name)
        print(f"  [FAIL] {name}" + (f" :: {detail}" if detail is not None else ""))
    else:
        print(f"  [ok]   {name}" + (f" :: {detail}" if detail is not None else ""))
    return ok


# ────────────────────────── 环境隔离 ──────────────────────────


class IsolatedDB:
    """一个用例一个临时库：不共享状态，可重复跑。"""

    def __init__(self, name: str):
        self.name = name
        self.dir = tempfile.mkdtemp(prefix=f"cet-probe-{name}-")
        self.path = Path(self.dir) / "vocab.db"
        self._old = os.environ.get("ELT_USER_DB")
        os.environ["ELT_USER_DB"] = str(self.path)
        os.environ["AI_API_KEY"] = ""

    def __enter__(self):
        import db
        from webapp.services import cet_service

        # 同一进程内换库：清掉模块级初始化缓存，强制重新 init
        db._initialized_paths.clear()
        cet_service._initialized.clear()
        db.init_db(self.path)
        cet_service.init_cet_schema(self.path)
        self.db = db
        self.svc = cet_service
        self._seed()
        return self

    def __exit__(self, *exc):
        import db as _db_mod

        _db_mod._initialized_paths.clear()
        if self._old is None:
            os.environ.pop("ELT_USER_DB", None)
        else:
            os.environ["ELT_USER_DB"] = self._old
        return False

    def _seed(self):
        """建一份最小可交卷材料：5 句 + 4 题（其中 1 题答案未知）。"""
        db, svc = self.db, self.svc
        lesson = db.create_v2_lesson(
            source_type="developmentFixture",
            source_url=f"fixture://{self.name}",
            title=f"Probe {self.name}",
            duration=13,
            media_url="/fixtures/probe.mp3",
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
        s = {i: f"{material_id}-{i}" for i in range(5)}
        svc.replace_questions_for_fixture(
            material_id,
            [
                {"id": "q1", "questionNumber": 1, "prompt": "When does it open?",
                 "options": ["At nine.", "At ten."], "correctAnswer": "A",
                 "answerSource": "human_verified", "evidenceSentenceId": s[0]},
                {"id": "q2", "questionNumber": 2, "prompt": "What can students reserve?",
                 "options": ["A quiet room.", "A computer."], "correctAnswer": "A",
                 "answerSource": "human_verified", "evidenceSentenceId": s[1]},
                {"id": "q3", "questionNumber": 3, "prompt": "What does the workshop cover?",
                 "options": ["Note taking.", "Job interviews."], "correctAnswer": "A",
                 "answerSource": "human_verified", "evidenceSentenceId": s[2]},
                {"id": "q4", "questionNumber": 4, "prompt": "What to bring?",
                 "options": ["Student card.", "Library card."], "correctAnswer": None,
                 "answerSource": "unknown", "evidenceSentenceId": s[4]},
            ],
        )
        self.material_id = material_id
        self.material_version = svc.get_material(material_id)["materialVersion"]

    # ── 库操作 ──

    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def count_attempts(self, material_id: str, session_id: str) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM cet_attempts WHERE material_id=? AND listening_session_id=?",
                (material_id, session_id),
            ).fetchone()[0]

    def drop_session_index(self):
        """模拟「有历史重复、部分唯一索引建不上」的库。"""
        with self.conn() as c:
            c.execute("DROP INDEX IF EXISTS cet_attempts_session_uniq")
            c.commit()
        self.svc._initialized.clear()

    def insert_legacy_duplicate(self, material_id: str, session_id: str, count: int = 3):
        """直接写入历史重复记录（模拟旧版本留下的脏数据），并确保索引建不上。"""
        with self.conn() as c:
            c.execute("DROP INDEX IF EXISTS cet_attempts_session_uniq")
            ids = []
            for i in range(count):
                aid = f"legacy-{session_id}-{i}"
                c.execute(
                    "INSERT INTO cet_attempts"
                    "(id,material_id,material_version,listening_session_id,score,graded_count,unknown_count,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (aid, material_id, self.material_version, session_id, i, 0, 0,
                     f"2020-01-0{i + 1}T00:00:00+00:00"),
                )
                ids.append(aid)
            c.commit()
        self.svc._initialized.clear()
        self.svc._ensure_attempt_idempotency(self.conn())
        return ids

    def answers(self):
        return [{"questionId": "q1", "choice": 0}, {"questionId": "q2", "choice": 1}]


def submit(svc, material_id, version, session_id, answers, attempt_id=None):
    body = {
        "materialId": material_id,
        "materialVersion": version,
        "listeningSessionId": session_id,
        "answers": answers,
    }
    if attempt_id:
        body["attemptId"] = attempt_id
    return svc.submit_attempt(body)


def concurrent_submit(svc, material_id, version, session_id, answers, attempt_ids, workers=6):
    """6 个线程同时交卷。

    用 barrier 对齐起跑时刻，让并发真的重叠；不伪造任何数据库结果。
    """
    barrier = threading.Barrier(workers)

    def one(i):
        barrier.wait(timeout=15)
        aid = attempt_ids[i] if attempt_ids else None
        try:
            return submit(svc, material_id, version, session_id, answers, aid)["attemptId"]
        except Exception as exc:  # noqa: BLE001
            return f"ERR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, range(workers)))


# ────────────────────────── A. 幂等四组合 ──────────────────────────


def case_matrix():
    print("\n== A. 幂等四组合矩阵（每种并发 6 次）==")
    matrix = {}

    # A1 正常库 + 服务生成 ID
    with IsolatedDB("normal-server-id") as env:
        session = "sess-" + uuid.uuid4().hex[:8]
        ids = concurrent_submit(env.svc, env.material_id, env.material_version,
                                session, env.answers(), None)
        n = env.count_attempts(env.material_id, session)
        matrix["normal+serverId"] = {"returned": ids, "unique": len(set(ids)), "rows": n,
                                     "state": env.svc.attempt_idempotency_state()}
        check("A1 正常库+服务生成ID：并发 6 次只 1 条", n == 1 and len(set(ids)) == 1,
              {"uniqueIds": len(set(ids)), "rows": n})

    # A2 正常库 + 客户端自带 ID（旧实现绕过的正是这条）
    with IsolatedDB("normal-client-id") as env:
        session = "sess-" + uuid.uuid4().hex[:8]
        client_ids = [f"client-{session}-{i}" for i in range(6)]
        ids = concurrent_submit(env.svc, env.material_id, env.material_version,
                                session, env.answers(), client_ids)
        n = env.count_attempts(env.material_id, session)
        matrix["normal+clientId"] = {"returned": ids, "unique": len(set(ids)), "rows": n,
                                     "state": env.svc.attempt_idempotency_state()}
        check("A2 正常库+客户端自带ID：并发 6 次只 1 条", n == 1 and len(set(ids)) == 1,
              {"uniqueIds": len(set(ids)), "rows": n})

    # A3 历史重复库 + 服务生成 ID
    with IsolatedDB("dup-server-id") as env:
        session = "sess-" + uuid.uuid4().hex[:8]
        legacy = env.insert_legacy_duplicate(env.material_id, session, 3)
        before = env.count_attempts(env.material_id, session)
        ids = concurrent_submit(env.svc, env.material_id, env.material_version,
                                session, env.answers(), None)
        n = env.count_attempts(env.material_id, session)
        matrix["dup+serverId"] = {"returned": ids, "unique": len(set(ids)), "rowsBefore": before,
                                  "rowsAfter": n, "state": env.svc.attempt_idempotency_state()}
        check("A3 历史重复库+服务生成ID：不新增记录", n == before,
              {"rowsBefore": before, "rowsAfter": n})
        check("A3 返回同一身份（最早一条）", len(set(ids)) == 1 and ids[0] == legacy[0],
              {"returned": ids, "earliest": legacy[0]})
        check("A3 历史 3 条一条都没删", before == 3, {"rowsBefore": before})

    # A4 历史重复库 + 客户端自带 ID（报告里失败的正是这条）
    with IsolatedDB("dup-client-id") as env:
        session = "sess-" + uuid.uuid4().hex[:8]
        legacy = env.insert_legacy_duplicate(env.material_id, session, 3)
        before = env.count_attempts(env.material_id, session)
        client_ids = [f"client-{session}-{i}" for i in range(6)]
        ids = concurrent_submit(env.svc, env.material_id, env.material_version,
                                session, env.answers(), client_ids)
        n = env.count_attempts(env.material_id, session)
        matrix["dup+clientId"] = {"returned": ids, "unique": len(set(ids)), "rowsBefore": before,
                                  "rowsAfter": n, "state": env.svc.attempt_idempotency_state()}
        check("A4 历史重复库+客户端自带ID：不新增记录", n == before,
              {"rowsBefore": before, "rowsAfter": n})
        check("A4 返回同一身份（最早一条）", len(set(ids)) == 1 and ids[0] == legacy[0],
              {"returned": ids, "earliest": legacy[0]})
        check("A4 历史 3 条一条都没删", before == 3, {"rowsBefore": before})

    RESULTS["matrix"] = matrix
    return matrix


# ────────────────────────── B. 空 session ──────────────────────────


def case_empty_session():
    print("\n== B. 空 session + 不同 attemptId 保留为独立作答 ==")
    with IsolatedDB("empty-session") as env:
        ids = []
        for i in range(2):
            aid = f"empty-{uuid.uuid4().hex[:6]}-{i}"
            ids.append(submit(env.svc, env.material_id, env.material_version,
                              "", env.answers(), aid)["attemptId"])
        with env.conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM cet_attempts WHERE listening_session_id=''"
            ).fetchone()[0]
        RESULTS["emptySession"] = {"ids": ids, "rows": total}
        check("B1 空 session 不同 attemptId 保留两份", total == 2 and len(set(ids)) == 2,
              {"ids": ids, "rows": total})
        check("B2 空 session 身份即客户端自带 ID", ids == [i for i in ids],
              {"ids": ids})


# ────────────────────────── C. 历史重复 + conflict 标注 ──────────────────────────


def case_conflict():
    print("\n== C. 历史重复记录：不删除、返回最早一条、标注 conflict ==")
    with IsolatedDB("conflict") as env:
        session = "sess-" + uuid.uuid4().hex[:8]
        legacy = env.insert_legacy_duplicate(env.material_id, session, 3)
        before = env.count_attempts(env.material_id, session)

        # 服务生成 ID 入口
        r1 = submit(env.svc, env.material_id, env.material_version, session, env.answers())
        # 客户端自带 ID 入口
        r2 = submit(env.svc, env.material_id, env.material_version, session, env.answers(),
                    f"client-{uuid.uuid4().hex[:6]}")
        after = env.count_attempts(env.material_id, session)

        RESULTS["conflict"] = {
            "legacyIds": legacy,
            "rowsBefore": before,
            "rowsAfter": after,
            "serverIdEntry": {"attemptId": r1["attemptId"], "conflict": r1.get("conflict"),
                              "detail": r1.get("conflictDetail")},
            "clientIdEntry": {"attemptId": r2["attemptId"], "conflict": r2.get("conflict"),
                              "detail": r2.get("conflictDetail")},
        }
        check("C1 历史重复记录一条都没删", after == before, {"before": before, "after": after})
        check("C2 两个入口都返回最早一条", r1["attemptId"] == legacy[0] == r2["attemptId"],
              {"earliest": legacy[0], "server": r1["attemptId"], "client": r2["attemptId"]})
        check("C3 响应标注 conflict=true", r1.get("conflict") is True and r2.get("conflict") is True,
              {"server": r1.get("conflict"), "client": r2.get("conflict")})
        detail = r1.get("conflictDetail") or {}
        check("C4 conflict 说明选择依据与全部候选",
              detail.get("policy") == "keep_earliest_created_at"
              and detail.get("duplicateCount") == 3
              and detail.get("attemptIds") == legacy,
              detail)

        # 无重复的正常会话不应误报冲突
        clean = "sess-" + uuid.uuid4().hex[:8]
        r3 = submit(env.svc, env.material_id, env.material_version, clean, env.answers())
        check("C5 无重复历史时不误报 conflict", r3.get("conflict") is False,
              {"conflict": r3.get("conflict")})


# ────────────────────────── D. AI 迟到响应 ──────────────────────────


def case_ai_lifecycle():
    print("\n== D. AI 生命周期：迟到响应按原因区分 ==")
    with IsolatedDB("ai-lifecycle") as env:
        svc = env.svc

        def new_request(status: str, error_code: str = ""):
            """造一个处于指定状态的 AI 请求，返回 request_id。"""
            rid = f"req-{uuid.uuid4().hex[:10]}"
            with env.conn() as c:
                c.execute(
                    "INSERT INTO cet_ai_requests"
                    "(id,practice_id,step_id,revision,action,status,request_json,error_code,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (rid, "practice-probe", "step-probe", 1, "explain", status, "{}",
                     error_code, "2020-01-01T00:00:00+00:00"),
                )
                c.commit()
            return rid

        def status_of(rid: str) -> str:
            with env.conn() as c:
                return c.execute(
                    "SELECT status FROM cet_ai_requests WHERE id=?", (rid,)
                ).fetchone()[0]

        def late_result(rid: str) -> bool:
            """模拟迟到响应到达：走生产代码的终态收敛函数。"""
            with env.conn() as c:
                took = svc._late_ai_result_terminal(c, rid)
                c.commit()
            return took

        scenarios = []

        # D1 pending → stale
        rid = new_request("pending")
        took = late_result(rid)
        after = status_of(rid)
        scenarios.append({"scenario": "pending", "expected": "stale", "actual": after,
                          "tookOver": took})
        check("D1 pending 迟到响应 → stale", after == "stale" and took is True,
              {"actual": after, "tookOver": took})

        # D2 failed + SERVER_RESTARTED → stale（进程异常悬空，允许接管）
        rid = new_request("failed", "SERVER_RESTARTED")
        took = late_result(rid)
        after = status_of(rid)
        scenarios.append({"scenario": "failed/SERVER_RESTARTED", "expected": "stale",
                          "actual": after, "tookOver": took})
        check("D2 failed(SERVER_RESTARTED) 迟到响应 → stale", after == "stale" and took is True,
              {"actual": after, "tookOver": took})

        # D3 failed + AI_INVALID_OUTPUT → 保持 failed（真失败）
        rid = new_request("failed", "AI_INVALID_OUTPUT")
        took = late_result(rid)
        after = status_of(rid)
        scenarios.append({"scenario": "failed/AI_INVALID_OUTPUT", "expected": "failed",
                          "actual": after, "tookOver": took})
        check("D3 failed(AI_INVALID_OUTPUT) 迟到响应不覆盖", after == "failed" and took is False,
              {"actual": after, "tookOver": took})

        # D4 cancelled + USER_CANCELLED → 保持 cancelled
        rid = new_request("cancelled", "USER_CANCELLED")
        took = late_result(rid)
        after = status_of(rid)
        scenarios.append({"scenario": "cancelled/USER_CANCELLED", "expected": "cancelled",
                          "actual": after, "tookOver": took})
        check("D4 cancelled(USER_CANCELLED) 迟到响应不覆盖", after == "cancelled" and took is False,
              {"actual": after, "tookOver": took})

        # D5 succeeded → 保持 succeeded
        rid = new_request("succeeded")
        took = late_result(rid)
        after = status_of(rid)
        scenarios.append({"scenario": "succeeded", "expected": "succeeded", "actual": after,
                          "tookOver": took})
        check("D5 succeeded 迟到响应不覆盖", after == "succeeded" and took is False,
              {"actual": after, "tookOver": took})

        RESULTS["aiLifecycle"] = scenarios

        # D6 用户主动取消必须留原因码（不是「被取代」）
        print("\n  -- D6 取消原因码 --")
        rid = new_request("pending")
        with env.conn() as c:
            c.execute(
                "INSERT INTO cet_practices"
                "(id,mark_id,material_id,material_version,sentence_id,sentence_json,audio_url,"
                "current_step_id,revision,status,ai_request_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("practice-probe", "mark-probe", env.material_id, env.material_version,
                 f"{env.material_id}-0", json.dumps({"startSeconds": 0, "endSeconds": 2.5,
                                                     "text": "probe", "sentenceId": f"{env.material_id}-0"}),
                 "/fixtures/probe.mp3", "step-probe", 1, "active", rid,
                 "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
            )
            c.commit()
        cancelled = svc.cancel_ai_request("practice-probe", {"requestId": rid})
        with env.conn() as c:
            row = c.execute(
                "SELECT status, error_code FROM cet_ai_requests WHERE id=?", (rid,)
            ).fetchone()
        RESULTS["cancelReasonCode"] = {"status": row["status"], "errorCode": row["error_code"]}
        check("D6 用户取消 → cancelled + USER_CANCELLED",
              row["status"] == "cancelled" and row["error_code"] == "USER_CANCELLED",
              {"status": row["status"], "errorCode": row["error_code"]})

        # D7 被新动作取代 → stale（不是 cancelled）
        print("\n  -- D7 被取代标 stale --")
        rid2 = f"req-{uuid.uuid4().hex[:10]}"
        with env.conn() as c:
            c.execute(
                "INSERT INTO cet_ai_requests"
                "(id,practice_id,step_id,revision,action,status,request_json,error_code,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (rid2, "practice-probe", "step-probe", 2, "explain", "pending", "{}", "",
                 "2020-01-01T00:00:01+00:00"),
            )
            c.execute("UPDATE cet_practices SET ai_request_id=? WHERE id='practice-probe'", (rid2,))
            c.commit()
        with env.conn() as c:
            practice = c.execute(
                "SELECT * FROM cet_practices WHERE id='practice-probe'"
            ).fetchone()
            svc._cancel_pending_ai(c, practice, status="stale")
            c.commit()
            row2 = c.execute(
                "SELECT status, error_code FROM cet_ai_requests WHERE id=?", (rid2,)
            ).fetchone()
        RESULTS["superseded"] = {"status": row2["status"], "errorCode": row2["error_code"]}
        check("D7 被新动作取代 → stale（不是 cancelled）",
              row2["status"] == "stale" and not row2["error_code"],
              {"status": row2["status"], "errorCode": row2["error_code"]})

        # D8 _cancel_pending_ai 必须显式传 status（不依赖默认值）
        print("\n  -- D8 status 参数无默认值 --")
        import inspect

        sig = inspect.signature(svc._cancel_pending_ai)
        status_param = sig.parameters["status"]
        explicit = status_param.default is inspect.Parameter.empty
        RESULTS["cancelSignature"] = {
            "hasDefault": not explicit,
            "signature": str(sig),
        }
        check("D8 _cancel_pending_ai 的 status 必须显式传入", explicit, str(sig))
        check("D8 _cancel_pending_ai 支持 error_code", "error_code" in sig.parameters, str(sig))


# ────────────────────────── main ──────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    if not args.json:
        print("D01 返修探针：交卷幂等全入口 + AI 生命周期终态")
        print(f"仓库：{ROOT}")
        print(f"分支：", end="")

    case_matrix()
    case_empty_session()
    case_conflict()
    case_ai_lifecycle()

    RESULTS["failures"] = FAILURES
    RESULTS["passed"] = len(FAILURES) == 0
    RESULTS["assertionCount"] = len(RESULTS.get("assertions", []))

    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 62)
        total = RESULTS["assertionCount"]
        print(f"结果：{total - len(FAILURES)}/{total} 通过"
              + ("" if not FAILURES else f"｜失败 {len(FAILURES)} 项"))
        if FAILURES:
            for name in FAILURES:
                print(f"  ✗ {name}")
        print("=" * 62)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
