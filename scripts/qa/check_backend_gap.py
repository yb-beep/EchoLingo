#!/usr/bin/env python3
"""四六级听力黑匣子 · 真实服务接口缺口报告（C 负责，给 B 用）。

为什么需要它：
    真实服务上跑契约测试，B 只会看到「34 failed」——一堆红字，不知道从哪下手。
    这份报告把失败**倒推成「哪些接口还没实现」**，按批次排好，
    B 照着实现即可，每实现一批就有一批测试转绿。

它做什么：
    1. 探测契约定义的每个 CET 接口在当前服务上的实际状态
    2. 把每条测试用例归类到它依赖的接口
    3. 按「实现批次」排序输出：材料 → 题目 → 标记 → 练习 → 复习
    4. 给出每批对应的命令与预计转绿数量
    5. 写 JSON 报告，便于与上一轮对比进展

重要口径：
    - 404 = 接口未实现（B 待做）
    - 5xx = 已实现但内部出错（B 待修）
    - 2xx/4xx(业务错误) = 已实现
    **不要**把「接口返回 400 参数错误」当成未实现 —— 那是正确的业务校验。

用法：
    .venv/bin/python scripts/qa/check_backend_gap.py
    .venv/bin/python scripts/qa/check_backend_gap.py --json
    .venv/bin/python scripts/qa/check_backend_gap.py --base http://127.0.0.1:5173

退出码：0 无关口（全部已实现或仅缺料）｜1 仍有接口未实现
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / ".cet-qa" / "gap"
MID = "cet6-202512-conv2"

# ────────────────────────── 接口清单（分批） ──────────────────────────
# 顺序即实现顺序：先材料与句子（一切的基础），再题目、标记，最后练习与复习。

BATCHES: list[dict] = [
    {
        "batch": 1,
        "name": "材料与句子",
        "why": "其它所有接口都依赖材料身份与句子时间轴，必须最先通",
        "endpoints": [
            ("GET", f"/api/cet/materials", {}),
            ("GET", f"/api/cet/materials/{MID}", {"version": "v1"}),
            ("GET", f"/api/cet/materials/{MID}/audio", {}),
            ("GET", f"/api/cet/materials/{MID}/questions", {"version": "v1"}),
        ],
        "unlocks": ["test_materials_list_shape", "test_material_detail_has_sentences",
                    "test_sentence_timeline_is_valid", "test_audio_asset_is_served",
                    "test_questions_hide_answers_before_attempt", "test_question_numbers_are_unique"],
    },
    {
        "batch": 2,
        "name": "交卷与判题",
        "why": "做题闭环：交卷后才返回答案与证据",
        "endpoints": [("POST", "/api/cet/attempts", {})],
        "unlocks": ["test_attempt_returns_answers_and_evidence",
                    "test_attempt_unknown_answer_not_guessed",
                    "test_duplicate_attempt_is_idempotent",
                    "test_attempt_cannot_change_existing_answers",
                    "test_concurrent_attempts_create_one_record"],
    },
    {
        "batch": 3,
        "name": "标记与确认",
        "why": "空格标记与句子吸附，回听页的基础",
        "endpoints": [
            ("POST", "/api/cet/marks", {}),
            ("GET", "/api/cet/marks", {"materialId": MID}),
            ("PATCH", "/api/cet/marks/probe-id", {}),
        ],
        "unlocks": ["test_mark_create_and_list", "test_duplicate_mark_event_does_not_create_record",
                    "test_mark_out_of_range_rejected", "test_mark_negative_time_rejected",
                    "test_mark_confirm_and_adjust", "test_mark_confirm_invalid_sentence_rejected",
                    "test_marks_isolated_by_material",
                    "test_concurrent_marks_from_different_events_all_recorded"],
    },
    {
        "batch": 4,
        "name": "练习与带练",
        "why": "反馈驱动带练：两类反馈走不同动作，过期结果要失效",
        "endpoints": [
            ("POST", "/api/cet/practices", {}),
            ("GET", "/api/cet/practices/probe-id", {}),
            ("POST", "/api/cet/practices/probe-id/events", {}),
            ("POST", "/api/cet/practices/probe-id/next", {}),
            ("POST", "/api/cet/practices/probe-id/local", {}),
            ("POST", "/api/cet/practices/probe-id/finish", {}),
        ],
        "unlocks": ["test_practice_requires_confirmed_mark", "test_practice_create_and_restore_same_id",
                    "test_feedback_leads_to_different_actions",
                    "test_heard_feedback_hides_text_and_plays_original_speed",
                    "test_stale_step_result_is_rejected", "test_local_help_works_without_model",
                    "test_playback_events_dedupe_and_count_time", "test_invalid_event_type_rejected",
                    "test_finish_is_idempotent_and_latest_feedback_wins",
                    "test_transfer_returns_real_candidates", "test_transfer_no_candidate_still_saves",
                    "test_concurrent_duplicate_events_count_once",
                    "test_concurrent_stale_and_fresh_next_do_not_corrupt_state"],
    },
    {
        "batch": 5,
        "name": "卡点与复习",
        "why": "十分钟复习：条目绑定原练习 ID，跳过不等于听懂",
        "endpoints": [
            ("GET", "/api/cet/review-items", {"materialId": MID}),
            ("POST", "/api/cet/reviews", {}),
            ("GET", "/api/cet/reviews/probe-id", {}),
            ("POST", "/api/cet/reviews/probe-id/actions", {}),
        ],
        "unlocks": ["test_review_items_reflect_latest_feedback",
                    "test_review_session_binds_original_practice_id",
                    "test_review_skip_keeps_item_pending",
                    "test_review_pause_does_not_accumulate_time"],
    },
]

# 不依赖具体接口、本就该在任一环境通过的用例
ALWAYS_PASS = ["test_report_target_environment"]


def probe(base: str, method: str, path: str, timeout: float = 5.0) -> tuple[int, str]:
    """探测一个接口。只看「路径存不存在」，不追求业务正确。

    404 与 405 都算未实现：405 说明路径存在但方法没注册，对 B 同样是待做。
    2xx 与 4xx（除 404/405）算已实现 —— 参数错是正常的业务校验。
    """
    url = f"{base}{path}"
    data = b"{}" if method in ("POST", "PATCH", "PUT") else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


# FastAPI 在「路由根本没注册」时返回的就是这个固定体。
# 而「路由存在、但资源不存在」会带业务错误体（如 code: MARK_NOT_FOUND）。
# 只看状态码 404 会把这两件事混为一谈 —— 那样 Mock 上的探针也会被误报成未实现。
_FASTAPI_BARE_404 = re.compile(r'^\s*\{\s*"detail"\s*:\s*"Not Found"\s*\}\s*$')


def is_route_missing(code: int, body: str) -> bool:
    """判断「路由不存在」而不是「资源不存在」。"""
    if code == 405:
        return True          # 路径在、方法没注册，对 B 同样是待做
    if code != 404:
        return False
    return bool(_FASTAPI_BARE_404.match(body or ""))


def classify(code: int, body: str = "") -> str:
    if code == 0:
        return "unreachable"
    if is_route_missing(code, body):
        return "missing"
    if code >= 500:
        return "erroring"
    return "implemented"


def main() -> int:
    parser = argparse.ArgumentParser(description="真实服务接口缺口报告")
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    # 服务可达性：不假定有 /health —— Mock 没有这个端点。
    # 用契约里的第一个接口探活，任何 HTTP 响应都说明服务活着。
    code, body = probe(base, "GET", "/api/cet/materials")
    if code == 0:
        print(f"X  服务不可达 {base}")
        print("   先启动：scripts/qa/start_cet.sh")
        return 1

    health: dict = {}
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass   # Mock 或其它目标可能没有 /health，不影响报告

    results = []
    for batch in BATCHES:
        rows = []
        for method, path, query in batch["endpoints"]:
            qs = "?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
            code, body = probe(base, method, f"{path}{qs}")
            state = classify(code, body)
            rows.append({
                "method": method,
                "path": path + qs,
                "status": code,
                "state": state,
                "sample": body[:120],
                # 路由在但探针资源不存在 —— 说明接口已实现，只是探针 ID 是假的
                "probeResourceMissing": state == "implemented" and code == 404,
            })
        results.append({**batch, "rows": rows})

    total_eps = sum(len(b["rows"]) for b in results)
    missing = sum(1 for b in results for r in b["rows"] if r["state"] == "missing")
    erroring = sum(1 for b in results for r in b["rows"] if r["state"] == "erroring")
    done = total_eps - missing - erroring

    ai_missing = (
        (health.get("environment") or {}).get("keys", {}).get("AI_API_KEY") == "missing"
    )

    if args.json:
        print(json.dumps({
            "base": base, "time": datetime.now().isoformat(timespec="seconds"),
            "summary": {"total": total_eps, "implemented": done,
                        "missing": missing, "erroring": erroring},
            "aiKeyMissing": ai_missing,
            "batches": results,
        }, ensure_ascii=False, indent=2))
    else:
        print("四六级听力黑匣子 · 真实服务接口缺口报告")
        print(f"目标 {base}｜契约 v1｜材料 {MID}")
        print("-" * 74)
        print(f"接口总数 {total_eps}：已实现 {done}｜未实现 {missing}｜报错 {erroring}")
        if ai_missing:
            print("注意：AI_API_KEY 未配置，依赖模型的用例在真实服务上必然失败（非 B 的代码问题）")
        print()

        for b in results:
            states = [r["state"] for r in b["rows"]]
            if all(s == "implemented" for s in states):
                mark = "OK"
            elif all(s == "missing" for s in states):
                mark = "--"
            else:
                mark = "~~"
            state_label = {"OK": "已实现", "--": "未开始", "~~": "部分完成"}[mark]
            print(f"[{mark}] 批次 {b['batch']} · {b['name']}（{state_label}）")
            print(f"      {b['why']}")
            for r in b["rows"]:
                icon = {"implemented": "OK", "missing": "--", "erroring": "X ",
                        "unreachable": "?"}[r["state"]]
                print(f"        [{icon}] {r['method']:<6} {r['path']:<48} {r['status']}")
            if mark != "OK":
                print(f"        完成后应转绿：{len(b['unlocks'])} 条用例")
                for u in b["unlocks"]:
                    print(f"          · {u}")
            print()

        # 下一步
        next_batch = next((b for b in results
                           if any(r["state"] != "implemented" for r in b["rows"])), None)
        print("-" * 74)
        if next_batch is None:
            print("全部接口已实现。下一步检查业务语义（flow 层）。")
        else:
            print(f"建议先做：批次 {next_batch['batch']} · {next_batch['name']}")
            print(f"  未实现接口：")
            for r in next_batch["rows"]:
                if r["state"] != "implemented":
                    print(f"    {r['method']:<6} {r['path']}")
            print(f"\n  实现后跑：")
            print(f"    CET_API_BASE={base} .venv/bin/python -m pytest \\")
            print(f"      tests/test_cet_contract.py -m contract -v")
            print(f"  再用本脚本确认该批转绿。")

    # 写报告
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "base": base,
        "time": datetime.now().isoformat(timespec="seconds"),
        "summary": {"total": total_eps, "implemented": done,
                    "missing": missing, "erroring": erroring},
        "aiKeyMissing": ai_missing,
        "batches": results,
        "alwaysPass": ALWAYS_PASS,
    }
    path = REPORT_DIR / "latest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.json:
        print(f"\n报告已写入：{path.relative_to(ROOT)}")

    return 0 if missing == 0 and erroring == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
