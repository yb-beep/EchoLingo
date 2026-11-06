#!/usr/bin/env python3
"""D01 返修 · 真实 HTTP 探针（真实路由 + 真实数据库 + 真并发）。

和 probe_idempotency.py 的分工：
  probe_idempotency.py  直调业务函数，逐个隔离库，覆盖四组合 + 生命周期语义
  本脚本                打真实 HTTP，覆盖「历史重复库 + 两种 ID 入口」的真并发

用法（需要一个跑着**你自己代码**的服务 + 一份可随意写的库）：
    # 1) 起服务（用私有库，别用日常库）
    cp .cet-qa/data/vocab.db /tmp/probe-db/vocab.db
    PORT=5281 ELT_DATA_DIR=/tmp/probe-db scripts/qa/start_cet.sh
    # 2) 打探针
    .venv/bin/python scripts/cet/probe_idempotency_http.py --base http://127.0.0.1:5281

断言：
  H1 正常库 + 服务生成 ID，并发 6 次 → 1 条
  H2 正常库 + 客户端自带 ID，并发 6 次 → 1 条
  H3 历史重复库（索引建不上）+ 服务生成 ID，并发 6 次 → 不新增
  H4 历史重复库（索引建不上）+ 客户端自带 ID，并发 6 次 → 不新增（D01 原缺陷）
  H5 历史重复记录不被删除，返回最早一条，响应标注 conflict: true
  H6 空 session + 不同 attemptId → 保留为独立作答

退出码：0 全部通过｜1 有断言失败
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
FAILURES: list[str] = []
RESULTS: dict = {}
TIMEOUT = 30


def check(name: str, ok: bool, detail=None) -> bool:
    RESULTS.setdefault("assertions", []).append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        FAILURES.append(name)
        print(f"  [FAIL] {name} :: {detail}")
    else:
        print(f"  [ok]   {name} :: {detail}")
    return ok


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def get(self, path, **kw):
        return requests.get(f"{self.base}{path}", timeout=TIMEOUT, **kw)

    def post(self, path, body):
        return requests.post(f"{self.base}{path}", json=body, timeout=TIMEOUT)


def pick_material(api: Api) -> dict:
    """挑一份「有题、且详情版本可用」的材料。

    注意：列表端点的 materialVersion 与详情端点可能不一致（既有问题，不在 D01 范围），
    交卷校验用的是详情端点版本，所以这里以详情为准。
    """
    r = api.get("/api/cet/materials")
    r.raise_for_status()
    for item in r.json()["items"]:
        mid = item["materialId"]
        q = api.get(f"/api/cet/materials/{mid}/questions")
        if q.status_code != 200 or not q.json().get("items"):
            continue
        detail = api.get(f"/api/cet/materials/{mid}")
        if detail.status_code != 200:
            continue
        material = detail.json().get("material") or {}
        version = material.get("materialVersion") or material.get("version")
        if version:
            return {"materialId": mid, "materialVersion": version}
    raise SystemExit("没有可交卷的材料，无法跑探针")


def answers_for(api: Api, material_id: str) -> list[dict]:
    items = api.get(f"/api/cet/materials/{material_id}/questions").json()["items"]
    return [{"questionId": q["questionId"], "choice": 0} for q in items[:2]]


def concurrent_post(api: Api, payloads: list[dict], workers: int = 6) -> list[dict]:
    """6 个线程同时 POST /attempts，用 barrier 对齐起跑，让并发真的重叠。"""
    barrier = threading.Barrier(workers)

    def one(i):
        barrier.wait(timeout=30)
        try:
            r = api.post("/api/cet/attempts", payloads[i])
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {"raw": r.text[:200]}
            return {"status": r.status_code, "body": body}
        except Exception as exc:  # noqa: BLE001
            return {"status": -1, "body": {"error": f"{type(exc).__name__}: {exc}"}}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, range(workers)))


def ids_of(results: list[dict]) -> list[str]:
    out = []
    for r in results:
        body = r.get("body") or {}
        out.append(str(body.get("attemptId") or f"ERR{r['status']}"))
    return out


def db_query(path: Path, sql: str, params=()):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def db_exec(path: Path, sql: str, params=()):
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5281")
    parser.add_argument("--db", required=True, help="服务正在使用的 SQLite 文件路径（探针直接读它核对落库）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    api = Api(args.base)
    if api.get("/health").status_code != 200:
        raise SystemExit(f"服务不可达：{args.base}")

    material = pick_material(api)
    mid, mver = material["materialId"], material["materialVersion"]
    answers = answers_for(api, mid)
    print(f"探针目标：{args.base}｜库 {db_path}")
    print(f"材料：{mid} @ {mver}｜题目 {len(answers)} 道\n")

    def rows(session: str) -> int:
        got = db_query(
            db_path,
            "SELECT COUNT(*) AS n FROM cet_attempts WHERE material_id=? AND listening_session_id=?",
            (mid, session),
        )
        return got[0]["n"]

    # ── H1 正常库 + 服务生成 ID ──
    print("== H1 正常库 + 服务生成 ID（并发 6）==")
    s1 = f"probe-h1-{uuid.uuid4().hex[:8]}"
    payloads = [{"materialId": mid, "materialVersion": mver,
                 "listeningSessionId": s1, "answers": answers} for _ in range(6)]
    res = concurrent_post(api, payloads)
    ids = ids_of(res)
    n1 = rows(s1)
    check("H1 并发 6 次只落 1 条", n1 == 1 and len(set(ids)) == 1,
          {"rows": n1, "uniqueIds": len(set(ids)), "statuses": [r["status"] for r in res]})
    RESULTS["H1"] = {"ids": ids, "rows": n1}

    # ── H2 正常库 + 客户端自带 ID ──
    print("\n== H2 正常库 + 客户端自带 ID（并发 6）==")
    s2 = f"probe-h2-{uuid.uuid4().hex[:8]}"
    payloads = [{"attemptId": f"client-{s2}-{i}", "materialId": mid, "materialVersion": mver,
                 "listeningSessionId": s2, "answers": answers} for i in range(6)]
    res = concurrent_post(api, payloads)
    ids = ids_of(res)
    n2 = rows(s2)
    check("H2 并发 6 次只落 1 条", n2 == 1 and len(set(ids)) == 1,
          {"rows": n2, "uniqueIds": len(set(ids)), "statuses": [r["status"] for r in res]})
    RESULTS["H2"] = {"ids": ids, "rows": n2}

    # ── H3/H4 历史重复库（索引建不上）──
    print("\n== H3/H4 历史重复库（部分唯一索引建不上）==")
    db_exec(db_path, "DROP INDEX IF EXISTS cet_attempts_session_uniq")
    print("  已删除唯一索引，模拟 needs_dedup 库")

    for tag, with_client_id in (("H3", False), ("H4", True)):
        session = f"probe-{tag.lower()}-{uuid.uuid4().hex[:8]}"
        legacy = []
        for i in range(3):
            aid = f"legacy-{session}-{i}"
            db_exec(
                db_path,
                "INSERT INTO cet_attempts"
                "(id,material_id,material_version,listening_session_id,score,graded_count,"
                "unknown_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (aid, mid, mver, session, 0, 0, 0, f"2020-01-0{i + 1}T00:00:00+00:00"),
            )
            legacy.append(aid)
        before = rows(session)
        if with_client_id:
            payloads = [{"attemptId": f"client-{session}-{i}", "materialId": mid,
                         "materialVersion": mver, "listeningSessionId": session,
                         "answers": answers} for i in range(6)]
            label = "历史重复库 + 客户端自带 ID"
        else:
            payloads = [{"materialId": mid, "materialVersion": mver,
                         "listeningSessionId": session, "answers": answers} for _ in range(6)]
            label = "历史重复库 + 服务生成 ID"
        res = concurrent_post(api, payloads)
        ids = ids_of(res)
        after = rows(session)
        check(f"{tag} {label}：并发 6 次不新增记录", after == before,
              {"rowsBefore": before, "rowsAfter": after,
               "statuses": [r["status"] for r in res]})
        check(f"{tag} 返回同一身份且为最早一条",
              len(set(ids)) == 1 and ids[0] == legacy[0],
              {"returned": sorted(set(ids)), "earliest": legacy[0]})
        check(f"{tag} 历史 3 条一条都没删", before == 3, {"rowsBefore": before})
        check(f"{tag} 响应标注 conflict=true",
              all((r["body"] or {}).get("conflict") is True for r in res),
              {"conflictFlags": [(r["body"] or {}).get("conflict") for r in res]})
        RESULTS[tag] = {"label": label, "ids": ids, "rowsBefore": before,
                        "rowsAfter": after, "legacy": legacy}

    # ── H5 冲突说明可追溯 ──
    print("\n== H5 冲突说明可追溯 ==")
    detail = (res[0]["body"] or {}).get("conflictDetail") or {}
    check("H5 conflictDetail 给出选择依据、候选与时间",
          detail.get("policy") == "keep_earliest_created_at"
          and detail.get("duplicateCount") == 3
          and len(detail.get("attemptIds") or []) == 3,
          detail)
    RESULTS["H5"] = detail

    # ── H6 空 session 独立作答 ──
    print("\n== H6 空 session + 不同 attemptId ==")
    empty_ids = []
    submitted = []
    for i in range(2):
        aid = f"empty-{uuid.uuid4().hex[:8]}-{i}"
        submitted.append(aid)
        r = api.post("/api/cet/attempts", {
            "attemptId": aid, "materialId": mid, "materialVersion": mver,
            "listeningSessionId": "", "answers": answers,
        })
        empty_ids.append((r.status_code, (r.json() or {}).get("attemptId")))
    # 只核本轮这两个 ID（可重跑：不把上一轮留下的行算进来）
    placeholders = ",".join("?" * len(submitted))
    stored = db_query(
        db_path,
        f"SELECT id FROM cet_attempts WHERE material_id=? AND listening_session_id='' "
        f"AND id IN ({placeholders})",
        (mid, *submitted),
    )
    ok = (
        all(s in (200, 201) for s, _ in empty_ids)
        and len(stored) == 2
        and [i for _, i in empty_ids] == submitted
    )
    check("H6 空 session 不同 attemptId 保留两份（不合并）", ok,
          {"responses": empty_ids, "storedRows": len(stored), "submitted": submitted})
    RESULTS["H6"] = {"responses": empty_ids, "storedRows": len(stored)}

    RESULTS["failures"] = FAILURES
    RESULTS["passed"] = not FAILURES
    RESULTS["assertionCount"] = len(RESULTS["assertions"])
    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    else:
        total = RESULTS["assertionCount"]
        print("\n" + "=" * 62)
        print(f"结果：{total - len(FAILURES)}/{total} 通过"
              + ("" if not FAILURES else f"｜失败 {len(FAILURES)} 项"))
        for name in FAILURES:
            print(f"  ✗ {name}")
        print("=" * 62)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
