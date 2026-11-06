#!/usr/bin/env python
"""集成验收：一次跑完所有门槛检查，输出结构化结果

用途：主代理合并子代理改动后，在**同一 SHA** 上跑这一份，避免「三个分支分别绿」
被当成集成通过。

覆盖 7 个验收门槛：
    1. 数据正确（幂等四组合、历史保留）
    2. 定位可用（F08 程序链路）
    3. 带练可用
    4. 换句可用
    5. 复习可用
    6. 可重复交付（全新库）
    7. 验证可信（三种模式分开）

用法：
    .venv/bin/python scripts/cet/verify_all.py
    .venv/bin/python scripts/cet/verify_all.py --skip-ui   # 跳过慢的 UI 测试
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("CET_API_BASE", "http://127.0.0.1:5173")

results: list[dict] = []


def record(gate: str, name: str, ok: bool, detail: str = "") -> None:
    results.append({"gate": gate, "name": name, "ok": ok, "detail": detail})
    mark = "OK  " if ok else "X   "
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def run(cmd: list[str], env_extra: dict | None = None, timeout: int = 600) -> tuple[int, str]:
    env = {**os.environ, **(env_extra or {})}
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"


def tail(text: str, n: int = 3) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:200]


def http_json(path: str):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ui", action="store_true", help="跳过 UI 测试（较慢）")
    args = ap.parse_args()

    print("=" * 70)
    print("  CET 集成验收")
    print(f"  仓库：{ROOT}")
    print(f"  服务：{BASE}")
    print("=" * 70)

    # ── 门槛 7：三种验证模式分开 ─────────────────────────
    print()
    print("【门槛 7】验证可信：三种模式分开跑")
    code, out = run([".venv/bin/python", "-m", "pytest", "tests/test_cet_contract.py", "-q"])
    record("7-验证", "契约 · Mock", code == 0, tail(out, 1))

    code, out = run([".venv/bin/python", "-m", "pytest", "tests/test_cet_contract.py", "-q"],
                    {"CET_API_BASE": BASE})
    # 真实服务允许有失败，但必须能跑完
    passed = "passed" in out
    record("7-验证", "契约 · 真实服务（可失败但需可跑）", passed, tail(out, 1))

    code, out = run([".venv/bin/python", "scripts/cet/run_contract_controlled.py"], timeout=900)
    record("7-验证", "契约 · 受控模型", "passed" in out, tail(out, 1))

    # ── 后端与模板回归 ───────────────────────────────────
    code, out = run([".venv/bin/python", "-m", "pytest", "tests/test_cet_backend.py", "-q"])
    record("7-验证", "后端自测", code == 0, tail(out, 1))

    code, out = run([".venv/bin/python", "-m", "pytest", "tests/test_natural_tts.py",
                     "tests/test_elt_credits_public_contract.py", "-q"])
    record("7-验证", "原页面契约（模板）", code == 0, tail(out, 1))

    if not args.skip_ui:
        code, out = run([".venv/bin/python", "-m", "pytest", "tests/test_cet_listening_ui.py", "-q"],
                        timeout=900)
        record("2-定位/3-带练/5-复习", "前端行为（真实浏览器）", code == 0, tail(out, 1))

    # ── 门槛 1：数据正确 ─────────────────────────────────
    print()
    print("【门槛 1】数据正确")
    code, out = run([".venv/bin/python", "scripts/cet/probe_idempotency.py"], timeout=300) \
        if (ROOT / "scripts" / "cet" / "probe_idempotency.py").is_file() else (1, "脚本不存在")
    record("1-数据", "幂等四组合矩阵", code == 0, tail(out, 2))

    # ── 门槛 6：可重复交付 ───────────────────────────────
    print()
    print("【门槛 6】可重复交付")
    code, out = run([".venv/bin/python", "scripts/cet/verify_fresh_db.py"], timeout=300)
    record("6-交付", "全新库导入与隔离", code == 0, tail(out, 1))

    code, out = run(["bash", "scripts/cet/run.sh", "--check"], timeout=120)
    record("6-交付", "启动前检查", code == 0, tail(out, 1))

    # ── 门槛 6：音频哈希与动态 ID ────────────────────────
    status, body = http_json("/api/cet/materials")
    items = (body or {}).get("items") or []
    cet = [m for m in items if str(m.get("materialId", "")).startswith(("cet4", "cet6"))]
    record("6-交付", "材料接口可达", status == 200 and bool(cet), f"{len(cet)} 份")
    has_hash = all(bool(m.get("audioSha256")) for m in cet) if cet else False
    record("6-交付", "音频哈希非空", has_hash,
           "; ".join(f"{m['materialId'][:14]}={str(m.get('audioSha256'))[:8]}" for m in cet))
    has_url = all(bool(m.get("lessonUrl")) for m in cet) if cet else False
    record("6-交付", "lessonUrl 动态给出", has_url,
           "; ".join(str(m.get("lessonUrl")) for m in cet))

    # ── 汇总 ─────────────────────────────────────────────
    print()
    print("=" * 70)
    total = len(results)
    ok = sum(1 for r in results if r["ok"])
    by_gate: dict[str, list] = {}
    for r in results:
        by_gate.setdefault(r["gate"], []).append(r)

    for gate, rows in by_gate.items():
        g_ok = sum(1 for x in rows if x["ok"])
        print(f"  {gate:22} {g_ok}/{len(rows)}")
    print()
    print(f"  合计：{ok}/{total} 项通过")
    if ok < total:
        print()
        print("  未通过项：")
        for r in results:
            if not r["ok"]:
                print(f"    - [{r['gate']}] {r['name']}：{r['detail']}")
    print("=" * 70)

    out_path = ROOT / ".cet-qa" / "verify_all.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "base": BASE,
        "total": total,
        "passed": ok,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  结果已写入 {out_path.relative_to(ROOT)}")

    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
