"""C04 / D02：受控模型验证脚本（无需真实 API key）

为什么需要它：
    验收报告指出「补上模型成功响应后，后续断言仍暴露协议差异」，
    而执行方此前把 9 项 503 归为「需 key 才能过」——这个分类没被验证。

D02 的教训（上一版为什么无效）：
    上一版在 `cet_ai._call` 边界固定返回两个跨句短语
    （`displaying signs of weariness` / `call it a day`）。
    生产校验器 `cet_ai._validate_phrase()` 要求短语是**当前原句的连续子串**，
    这两个短语属于不同句子，于是六级材料 32 句全部 `AI_INVALID_OUTPUT`。
    所以「无 key 与受控模型都是 33/39，所以一致」是错的 ——
    受控成功路径**从未跑通**，只是把一种失败换成了另一种失败。

本版的四条硬要求（逐条对应 D02 返修要求）：
    1. fixture 从**输入原句**派生合法短语/分段（见 controlled_fixture.py）
    2. chunk 每段 ≤8 词、完整覆盖原句、不改写
    3. **主动断言确实成功**：explanation 非空 + keyPhrases 合法，
       并单独输出「explain 成功 N/32」的证据，不只看整套测试通过数
    4. 分别注入 成功 / 慢响应 / 格式错误 / 超时，
       并断言**对应路径确实发生**（不是只看状态码）

隔离要求：
    脚本使用**独立数据库与独立端口**，绝不写入日常使用库
    （上一版未设 `ELT_USER_DB`，会落到 `resources/vocab.db` 日常库）。

用法：
    .venv/bin/python scripts/cet/run_contract_controlled.py            # 全部
    .venv/bin/python scripts/cet/run_contract_controlled.py -k review  # 只跑某几项

边界（必须如实说明）：
    - 注入的只是**模型返回内容**，不是模型质量、速度或训练效果
    - 不能据此声称「真实模型已验证」
    - 结论只对「程序链路是否正确」有效
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts" / "cet"))

import controlled_fixture as cf  # noqa: E402

# ────────────────────────── 隔离环境 ──────────────────────────

# 隔离库模板：优先用项目自己的 QA 隔离库，其次日常库的**只读副本**。
ISOLATED_TEMPLATE = ROOT / ".cet-qa" / "data" / "vocab.db"
DAILY_DB = ROOT / "resources" / "vocab.db"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def prepare_isolated_env(workdir: Path) -> dict[str, str]:
    """建立隔离数据库与配置目录，返回注入给本进程的环境变量。

    关键点：必须显式设置 `ELT_USER_DB`。
    不设置时 `db.current_db_path()` 会回落到 `resources/vocab.db`，
    也就是**日常使用库** —— 受控脚本会往里写练习、标记、事件。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    isolated_db = (workdir / "vocab.db").resolve()
    config_dir = (workdir / "config").resolve()
    config_dir.mkdir(parents=True, exist_ok=True)

    if ISOLATED_TEMPLATE.exists():
        shutil.copy2(ISOLATED_TEMPLATE, isolated_db)
    elif DAILY_DB.exists():
        # 兜底：从日常库复制一份（只读复制，不改动原库）
        shutil.copy2(DAILY_DB, isolated_db)
    else:
        isolated_db.touch()

    if isolated_db == DAILY_DB.resolve():
        raise SystemExit(
            f"拒绝运行：隔离库解析到了日常库 {isolated_db}。"
            "请检查 ISOLATED_TEMPLATE / ELT_USER_DB。"
        )
    return {
        "ELT_USER_DB": str(isolated_db),
        "ELT_CONFIG_DIR": str(config_dir),
    }


def count_cet_tables(db_path: Path) -> int:
    """数日常库里的 CET 业务表数量。

    为什么单独数这个：`init_cet_schema()` 会建 **14 张** cet_* 表并写数据。
    如果旧脚本在「还没有 CET 表」的日常库上跑过，会把这些表建进真实用户库。
    仅比对 sha256 能发现变化，但说不清「变了什么」；
    单独数 CET 表能把「最危险的那类写入」变成一条可读的断言。
    """
    if not db_path.exists():
        return 0
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cet%'"
        ).fetchall()
        return len(rows)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def daily_db_fingerprint() -> dict[str, Any]:
    """日常库指纹：用于**运行前后对比**，证明脚本没写进日常库。

    只设环境变量不够 —— 任何一处忘了传 `ELT_USER_DB` 的调用路径
    （例如 create_app() 里走 db.DB_PATH 的后台任务）都会绕过它。
    真正可靠的保证是：跑之前记指纹，跑完再记一次，不一致就报错。
    """
    if not DAILY_DB.exists():
        return {"exists": False}
    stat = DAILY_DB.stat()
    digest = hashlib.sha256()
    with DAILY_DB.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "exists": True,
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "cetTables": count_cet_tables(DAILY_DB),
    }


def assert_daily_db_untouched(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """运行前后比对日常库；有任何变化都视为隔离失败。"""
    problems: list[str] = []
    if before.get("exists") != after.get("exists"):
        problems.append(
            f"日常库存在性变化：before={before.get('exists')} after={after.get('exists')}"
        )
        return problems
    if not before.get("exists"):
        return problems
    if before.get("sha256") != after.get("sha256"):
        problems.append(
            "日常库内容被修改："
            f"sha256 {before.get('sha256', '')[:16]} → {after.get('sha256', '')[:16]}"
        )
    # 单独把「CET 表数量」拎出来断言：即使 sha256 因 WAL/页布局变化而不同，
    # 这条也能明确回答「最危险的那类写入（CET 业务表）有没有发生」。
    before_cet = before.get("cetTables", 0)
    after_cet = after.get("cetTables", 0)
    if before_cet != after_cet:
        problems.append(
            f"日常库 CET 表数量变化：{before_cet} → {after_cet}"
            "（init_cet_schema 会建 14 张 cet_* 表，说明有 CET 数据写进了日常库）"
        )
    return problems


# ────────────────────────── 受控注入 ──────────────────────────


class InjectionState:
    """记录注入模式的命中情况，供「断言路径确实发生」使用。"""

    def __init__(self) -> None:
        self.mode = "success"
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, action: str, sentence: str, outcome: str, detail: str = "") -> None:
        with self._lock:
            self.calls.append(
                {
                    "seq": len(self.calls) + 1,
                    "action": action,
                    "mode": self.mode,
                    "sentence": sentence[:48],
                    "outcome": outcome,
                    "detail": detail,
                }
            )

    def count(self, mode: str, outcome: str | None = None) -> int:
        with self._lock:
            return sum(
                1
                for call in self.calls
                if call["mode"] == mode and (outcome is None or call["outcome"] == outcome)
            )


STATE = InjectionState()

MODE_ENV = "CET_CONTROLLED_MODE"
SLOW_SECONDS = float(os.environ.get("CET_CONTROLLED_SLOW_SEC", "1.5"))


def install_controlled_call() -> None:
    """在 cet_ai._call 边界替换为受控实现。

    四种模式由环境变量 `CET_CONTROLLED_MODE` 选择，供脚本分轮次注入：
        success  合法响应（默认）
        slow     慢响应：睡眠 SLOW_SECONDS 后返回合法响应
        invalid  格式错误：返回缺 explanation / 空 options 的非法结构
        timeout  超时：抛 AI_TIMEOUT
    """
    from webapp.services import cet_ai

    def _controlled(action: str, sentence: str, feedback: str, candidate):
        mode = os.environ.get(MODE_ENV, "success").strip().lower()
        STATE.mode = mode

        if mode == "slow":
            time.sleep(SLOW_SECONDS)
        elif mode == "timeout":
            STATE.record(action, sentence, "timeout", "抛 AI_TIMEOUT")
            raise cet_ai.CETAIError("AI_TIMEOUT", "受控响应：模型请求超时。", 504)
        elif mode == "invalid":
            STATE.record(action, sentence, "invalid", "返回结构非法，应被生产校验器拒绝")
            if action == "explain":
                # 缺 explanation：生产校验器必须拒绝
                return ({"explanation": "", "keyPhrases": []}, "controlled-model")
            if action == "chunk":
                # 只有 1 段：生产校验器要求 2–4 段
                return ({"chunks": [sentence]}, "controlled-model")
            return ({"question": "", "options": [], "correctAnswer": "Z"}, "controlled-model")

        if action == "explain":
            payload = {
                "explanation": cf.CONTROLLED_EXPLANATION,
                "keyPhrases": cf.build_key_phrases(sentence),
            }
            # 主动断言：fixture 必须能通过生产校验规则，否则立刻失败，
            # 而不是等整套测试给出一个含混的失败数。
            cf.assert_valid_explanation(payload, sentence)
            STATE.record(action, sentence, "success", f"keyPhrases={payload['keyPhrases']}")
            return (payload, "controlled-model")

        if action == "chunk":
            chunks = cf.build_chunks(sentence)
            if chunks is None:
                STATE.record(
                    action, sentence, "unsegmentable",
                    "该句在 2–4 段/≤8 词约束下无法合法分段",
                )
                raise cet_ai.CETAIError(
                    "AI_INVALID_OUTPUT", "受控响应：该句无法在契约约束内合法分段。"
                )
            payload = {"chunks": chunks}
            cf.assert_valid_chunks(payload, sentence)
            STATE.record(action, sentence, "success", f"chunks={len(chunks)}")
            return (payload, "controlled-model")

        if action == "transfer":
            payload = cf.build_transfer(candidate)
            cf.assert_valid_transfer(payload)
            STATE.record(action, sentence, "success", "transfer options=2")
            return (payload, "controlled-model")

        STATE.record(action, sentence, "unsupported", action)
        raise cet_ai.CETAIError("AI_INVALID_OUTPUT", f"受控响应未覆盖动作：{action}")

    cet_ai._call = _controlled


# ────────────────────────── 单路径断言 ──────────────────────────


def assert_paths_exercised(base: str) -> dict[str, Any]:
    """分别注入四条路径并**断言对应路径确实发生**。

    D02 要求原文：「分别注入成功 / 慢响应 / 格式错误 / 超时，
    并断言对应路径确实发生，不能只看整套测试通过数。」
    """
    import requests

    base = base.rstrip("/")
    material_id = "cet6-202512-conv2"
    report: dict[str, Any] = {"checks": [], "explainSuccess": None}

    def _material_version() -> str:
        body = requests.get(f"{base}/api/cet/materials/{material_id}", timeout=15).json()
        return str((body.get("material") or body)["materialVersion"])

    def _new_practice(version: str) -> str:
        questions = requests.get(
            f"{base}/api/cet/materials/{material_id}/questions", timeout=15
        ).json()["items"]
        session = f"controlled-{time.time_ns()}"
        requests.post(
            f"{base}/api/cet/attempts",
            json={
                "materialId": material_id,
                "materialVersion": version,
                "listeningSessionId": session,
                "answers": [{"questionId": q["questionId"], "choice": 0} for q in questions],
            },
            timeout=20,
        )
        mark = requests.post(
            f"{base}/api/cet/marks",
            json={
                "materialId": material_id,
                "materialVersion": version,
                "listeningSessionId": session,
                "pressedAtSec": 36.9,
            },
            timeout=15,
        ).json()
        requests.patch(
            f"{base}/api/cet/marks/{mark['markId']}",
            json={"confirmedSentenceId": mark["suggestedSentenceId"]},
            timeout=15,
        )
        practice = requests.post(
            f"{base}/api/cet/practices", json={"markId": mark["markId"]}, timeout=15
        ).json()
        return practice["practiceId"]

    def _ask(practice_id: str, feedback: str) -> Any:
        body = requests.get(f"{base}/api/cet/practices/{practice_id}", timeout=15).json()
        return requests.post(
            f"{base}/api/cet/practices/{practice_id}/next",
            json={
                "feedback": feedback,
                "stepId": body["currentStepId"],
                "materialVersion": body["materialVersion"],
            },
            timeout=60,
        )

    def _sentence_text(practice_id: str) -> str:
        body = requests.get(f"{base}/api/cet/practices/{practice_id}", timeout=15).json()
        return str((body.get("sentence") or {}).get("text") or "")

    version = _material_version()

    # ── 1. 成功路径：必须真的产出合法 explanation ──
    os.environ[MODE_ENV] = "success"
    practice_id = _new_practice(version)
    sentence_text = _sentence_text(practice_id)
    before = STATE.count("success", "success")
    started = time.time()
    response = _ask(practice_id, "meaning")
    elapsed_success = time.time() - started
    check: dict[str, Any] = {
        "path": "success",
        "status": response.status_code,
        "elapsedSec": round(elapsed_success, 3),
        "injectedCalls": STATE.count("success", "success") - before,
    }
    if response.status_code == 200:
        body = response.json()
        step = body.get("step") or {}
        data = step.get("data") or {}
        explanation = str(data.get("explanation") or "").strip()
        phrases = data.get("keyPhrases")
        check["action"] = step.get("action")
        check["explanationNonEmpty"] = bool(explanation)
        check["keyPhrases"] = phrases
        # 合法 = 每个短语都是**该句原文的连续子串**且 ≤8 词
        check["keyPhrasesValid"] = isinstance(phrases, list) and all(
            isinstance(item, str)
            and item
            and item in sentence_text
            and cf.word_count(item) <= cf.MAX_PHRASE_WORDS
            for item in (phrases or [])
        )
        ai_request = body.get("aiRequest") or {}
        check["aiRequestStatus"] = ai_request.get("status")
        report["explainSuccess"] = {
            "explanation": explanation[:80],
            "keyPhrases": phrases,
            "aiRequestStatus": ai_request.get("status"),
            "sentence": sentence_text[:80],
            "succeeded": bool(explanation)
            and check["keyPhrasesValid"]
            and ai_request.get("status") == "succeeded",
        }
    else:
        check["body"] = response.text[:300]
    report["checks"].append(check)

    # ── 2. 慢响应路径：必须确实慢 ──
    os.environ[MODE_ENV] = "slow"
    practice_id = _new_practice(version)
    before = STATE.count("slow", "success")
    started = time.time()
    response = _ask(practice_id, "meaning")
    elapsed_slow = time.time() - started
    report["checks"].append(
        {
            "path": "slow",
            "status": response.status_code,
            "elapsedSec": round(elapsed_slow, 3),
            "expectedMinSec": SLOW_SECONDS,
            "slowerThanSuccess": elapsed_slow > elapsed_success + SLOW_SECONDS * 0.5,
            "injectedCalls": STATE.count("slow", "success") - before,
        }
    )

    # ── 3. 格式错误路径：生产校验器必须拒绝，且不得写入步骤 ──
    os.environ[MODE_ENV] = "invalid"
    practice_id = _new_practice(version)
    before_step = requests.get(f"{base}/api/cet/practices/{practice_id}", timeout=15).json()
    before = STATE.count("invalid", "invalid")
    response = _ask(practice_id, "meaning")
    after_step = requests.get(f"{base}/api/cet/practices/{practice_id}", timeout=15).json()
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    report["checks"].append(
        {
            "path": "invalid",
            "status": response.status_code,
            "errorCode": detail.get("code"),
            "injectedCalls": STATE.count("invalid", "invalid") - before,
            "stepUnchanged": after_step.get("currentStepId") == before_step.get("currentStepId")
            and after_step.get("stepCount") == before_step.get("stepCount"),
        }
    )

    # ── 4. 超时路径：必须报 AI_TIMEOUT ──
    os.environ[MODE_ENV] = "timeout"
    practice_id = _new_practice(version)
    before = STATE.count("timeout", "timeout")
    response = _ask(practice_id, "meaning")
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    ai_status = requests.get(
        f"{base}/api/cet/practices/{practice_id}/ai-status", timeout=15
    ).json()
    report["checks"].append(
        {
            "path": "timeout",
            "status": response.status_code,
            "errorCode": detail.get("code"),
            "injectedCalls": STATE.count("timeout", "timeout") - before,
            "aiRequestStatus": (ai_status.get("aiRequest") or {}).get("status"),
        }
    )

    os.environ[MODE_ENV] = "success"
    report["injectionLog"] = STATE.calls[:40]
    report["injectionCounts"] = {
        mode: STATE.count(mode) for mode in ("success", "slow", "invalid", "timeout")
    }
    return report


def print_path_report(report: dict[str, Any]) -> list[str]:
    """打印单路径断言结果，返回失败项清单。"""
    failures: list[str] = []
    print("── 受控注入路径断言 ──")
    for check in report["checks"]:
        path = check["path"]
        if path == "success":
            ok = (
                check.get("status") == 200
                and check.get("explanationNonEmpty") is True
                and check.get("keyPhrasesValid") is True
                and check.get("aiRequestStatus") == "succeeded"
                and check.get("injectedCalls", 0) >= 1
            )
            print(
                f"  [{'PASS' if ok else 'FAIL'}] success   status={check.get('status')} "
                f"action={check.get('action')} explanation非空={check.get('explanationNonEmpty')} "
                f"keyPhrases合法={check.get('keyPhrasesValid')} "
                f"aiRequest={check.get('aiRequestStatus')} 注入调用={check.get('injectedCalls')}"
            )
            if not ok:
                failures.append(f"success 路径未真正成功：{json.dumps(check, ensure_ascii=False)[:400]}")
        elif path == "slow":
            ok = (
                check.get("status") == 200
                and check.get("slowerThanSuccess") is True
                and check.get("injectedCalls", 0) >= 1
            )
            print(
                f"  [{'PASS' if ok else 'FAIL'}] slow      status={check.get('status')} "
                f"{check.get('elapsedSec')}s（期望 ≥{check.get('expectedMinSec')}s）"
                f" 注入调用={check.get('injectedCalls')}"
            )
            if not ok:
                failures.append(f"slow 路径未发生：{json.dumps(check, ensure_ascii=False)[:400]}")
        elif path == "invalid":
            ok = (
                check.get("status", 0) >= 400
                and check.get("stepUnchanged") is True
                and check.get("injectedCalls", 0) >= 1
            )
            print(
                f"  [{'PASS' if ok else 'FAIL'}] invalid   status={check.get('status')} "
                f"code={check.get('errorCode')} 步骤未被污染={check.get('stepUnchanged')} "
                f"注入调用={check.get('injectedCalls')}"
            )
            if not ok:
                failures.append(f"invalid 路径未按预期被拒：{json.dumps(check, ensure_ascii=False)[:400]}")
        elif path == "timeout":
            ok = (
                check.get("status", 0) >= 400
                and check.get("errorCode") == "AI_TIMEOUT"
                and check.get("injectedCalls", 0) >= 1
            )
            print(
                f"  [{'PASS' if ok else 'FAIL'}] timeout   status={check.get('status')} "
                f"code={check.get('errorCode')} 注入调用={check.get('injectedCalls')}"
            )
            if not ok:
                failures.append(f"timeout 路径未按预期：{json.dumps(check, ensure_ascii=False)[:400]}")

    explain = report.get("explainSuccess")
    print()
    print("── explain 成功路径单独证据（不能只看总通过数）──")
    if explain and explain.get("succeeded"):
        print(f"  [PASS] 原句：{explain['sentence']}")
        print(f"  [PASS] explanation 非空：{explain['explanation']}")
        print(f"  [PASS] keyPhrases 合法：{explain['keyPhrases']}  aiRequest={explain['aiRequestStatus']}")
    else:
        print(f"  [FAIL] explain 未真正成功：{json.dumps(explain, ensure_ascii=False)[:500]}")
        failures.append("explain 成功路径未跑通")
    return failures


# ────────────────────────── 主流程 ──────────────────────────


def main() -> int:
    port = int(os.environ.get("CET_CONTROLLED_PORT") or _free_port())
    workdir = Path(
        os.environ.get("CET_CONTROLLED_WORKDIR")
        or tempfile.mkdtemp(prefix="cet-controlled-")
    )
    env_overrides = prepare_isolated_env(workdir)
    os.environ.update(env_overrides)

    # 运行前记录日常库指纹，跑完对比 —— 这是「没写进日常库」的硬证据
    daily_before = daily_db_fingerprint()

    install_controlled_call()

    import uvicorn
    from fastapi_server import create_app  # type: ignore

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        print("受控服务启动失败")
        return 1

    print(f"受控服务：{base}")
    print("模型响应来源：cet_ai._call 注入（非真实模型）")
    print(f"隔离库：{env_overrides['ELT_USER_DB']}")
    print(f"日常库（不得写入）：{DAILY_DB}")
    print()

    failures: list[str] = []
    try:
        path_report = assert_paths_exercised(base)
        failures.extend(print_path_report(path_report))
        evidence_path = workdir / "controlled-evidence.json"
        evidence_path.write_text(
            json.dumps(path_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n证据：{evidence_path}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"单路径断言异常：{type(exc).__name__}: {exc}")
        print(f"  [FAIL] 单路径断言异常：{type(exc).__name__}: {exc}")

    print()
    print("── 完整契约（受控模型）──")
    os.environ[MODE_ENV] = "success"
    env = {**os.environ, "CET_API_BASE": base}
    extra = sys.argv[1:]
    cmd = [sys.executable, "-m", "pytest", "tests/test_cet_contract.py", "-q", *extra]
    result = subprocess.run(cmd, cwd=str(ROOT), env=env)
    server.should_exit = True

    # 跑完比对日常库：内容有任何变化都算隔离失败
    daily_after = daily_db_fingerprint()
    isolation_problems = assert_daily_db_untouched(daily_before, daily_after)
    failures.extend(f"日常库被写入：{item}" for item in isolation_problems)

    print()
    print("── 隔离验证 ──")
    if isolation_problems:
        for item in isolation_problems:
            print(f"  [FAIL] {item}")
    else:
        digest = daily_after.get("sha256", "")[:16]
        cet_before = daily_before.get("cetTables", 0)
        cet_after = daily_after.get("cetTables", 0)
        print(f"  [PASS] 日常库未被写入（sha256 {digest} 前后一致）")
        print(f"  [PASS] 日常库 CET 表数量未变（{cet_before} → {cet_after}）")
    print(f"  隔离库：{env_overrides['ELT_USER_DB']}")
    print(f"  日常库：{DAILY_DB}")

    print()
    print("── 汇总 ──")
    print(f"  单路径断言失败：{len(failures)}")
    for item in failures:
        print(f"    - {item}")
    print(f"  契约退出码：{result.returncode}")
    if failures:
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
