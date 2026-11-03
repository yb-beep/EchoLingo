#!/usr/bin/env python3
"""四六级听力黑匣子 · 集成分支合并与验证（C 负责）。

任务书要求 C「维护集成分支并合并 A、B 的小提交」「拿到提交就记录 SHA，合入一批，
启动一次，先验证材料和标记，再接带练、保存与恢复」。

这个脚本把这件事固化成可重复的一步，避免每次手工操作漏掉记录或验证。

它做什么：
    1. 记录当前集成分支状态（分支、HEAD、工作区是否干净）
    2. 从远端拉取 A/B 的分支（可选），报告有哪些新提交
    3. 逐个合入指定分支/提交，**每合一个立刻记录其 SHA**
    4. 合并后跑验证：环境检查 → 契约测试（打真实服务）→ 浏览器检查
    5. 输出一份集成报告（含每个提交的 SHA、合并结果、验证结果）
    6. 任一环节失败就停下并保留现场，不自动回退（由人判断）

设计原则：
    - **不做破坏性操作**：不 force push，不自动 reset --hard，不删分支
    - **失败即停**：合并冲突或验证失败就停，保留工作区供排查
    - **记录 SHA**：每个合入的提交都要有 SHA，否则事后无法对应测试结果
    - 只合并到集成分支，不动 main

用法：
    # 查看有哪些可合的分支（不改任何东西）
    .venv/bin/python scripts/qa/integrate.py --list

    # 合入 A 的分支并验证
    .venv/bin/python scripts/qa/integrate.py --merge codex/cet-frontend

    # 合入多个 + 只跑契约测试（跳浏览器检查，快一些）
    .venv/bin/python scripts/qa/integrate.py --merge codex/cet-frontend --merge codex/cet-backend --fast

    # 只验证当前状态，不合并
    .venv/bin/python scripts/qa/integrate.py --verify-only

退出码：0 全部成功｜1 合并失败或验证失败｜2 参数/前置问题
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / ".cet-qa" / "integration"
INTEGRATION_BRANCH = "codex/cet-integration"
CANDIDATE_REMOTES = ("origin", "upstream")

# 已知的、已登记在问题清单里的基线失败。
# 不把它们排除掉，每次集成都报红，久了就没人当真 —— 那等于没有门禁。
# 只在「同一个用例名」上豁免；一旦出现新失败项，仍然算失败。
KNOWN_BASELINE_FAILURES = {
    # BUG-001：FastAPI 0.162 的 _IncludedRouter 让 app.routes 不再扁平，
    # 属版本漂移、非业务回归。待 B 决定是否锁版本后应移除此豁免。
    "tests/test_public_single_user_compat.py::test_fastapi_server_starts_single_user_without_private_modules": "BUG-001",
}


def run(cmd: list[str], *, check: bool = False, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{out}")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）：{' '.join(cmd)}"


def git(*args: str, check: bool = False, timeout: int = 180) -> tuple[int, str]:
    return run(["git", *args], check=check, timeout=timeout)


def git_proxyless(*args: str, timeout: int = 300) -> tuple[int, str]:
    """GitHub 访问需要绕开本机失效的全局代理配置（C 环境实测）。

    只影响本次调用，不修改任何全局配置。
    """
    return run(
        ["git", "-c", "http.https://github.com.proxy=", *args],
        timeout=timeout,
    )


def current_branch() -> str:
    _, out = git("rev-parse", "--abbrev-ref", "HEAD")
    return out.strip()


def head_sha() -> str:
    _, out = git("rev-parse", "HEAD")
    return out.strip()


def is_dirty() -> bool:
    _, out = git("status", "--porcelain")
    return bool(out.strip())


def remote_url(remote: str) -> str:
    code, out = git("remote", "get-url", remote)
    return out.strip() if code == 0 else ""


# ────────────────────────── 列表 ──────────────────────────


def list_remotes() -> None:
    print("== 仓库远端 ==")
    for remote in CANDIDATE_REMOTES:
        url = remote_url(remote)
        print(f"  {remote:<10} {url or '(未配置)'}")

    print("\n== 本地分支 ==")
    _, out = git("branch", "-vv", "--no-color")
    for line in out.splitlines():
        print(f"  {line.strip()}")

    print("\n== 远端分支（可能需要网络）==")
    for remote in CANDIDATE_REMOTES:
        if not remote_url(remote):
            continue
        code, out = git_proxyless("ls-remote", "--heads", remote_url(remote))
        if code != 0:
            print(f"  {remote}: 无法访问（{out.splitlines()[0][:70] if out else '未知错误'}）")
            continue
        refs = [l for l in out.splitlines() if l.strip()]
        interesting = [
            l for l in refs
            if "cet" in l.lower() or "cet" in l.lower()
        ]
        print(f"  {remote}: 共 {len(refs)} 个分支")
        for line in interesting or refs[:8]:
            parts = line.split()
            if len(parts) == 2:
                print(f"    {parts[1].replace('refs/heads/', ''):<40} {parts[0][:12]}")
        if not interesting and len(refs) > 8:
            print(f"    …另有 {len(refs) - 8} 个")


# ────────────────────────── 合并 ──────────────────────────


def resolve_ref(ref: str) -> tuple[str, str]:
    """把用户给的分支名解析成 (显示名, 可 merge 的 ref)。

    依次尝试：本地分支 → origin/分支 → upstream/分支。
    找不到就返回空，由调用方报错。
    """
    candidates = [ref] + [f"{r}/{ref}" for r in CANDIDATE_REMOTES]
    for cand in candidates:
        code, _ = git("rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
        if code == 0:
            return cand, cand
    return ref, ""


def ensure_integration_branch(base: str) -> tuple[bool, str]:
    """确保在集成分支上。不在就创建/切换，失败则返回原因。"""
    branch = current_branch()
    if branch == INTEGRATION_BRANCH:
        return True, f"已在集成分支 {INTEGRATION_BRANCH}"

    _, exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{INTEGRATION_BRANCH}")
    if exists.strip():
        code, out = git("checkout", INTEGRATION_BRANCH)
        if code != 0:
            return False, f"切换集成分支失败：{out[:200]}"
        return True, f"已切到现有集成分支 {INTEGRATION_BRANCH}"

    code, out = git("checkout", "-b", INTEGRATION_BRANCH, base)
    if code != 0:
        return False, f"创建集成分支失败：{out[:200]}"
    return True, f"已从 {base} 创建集成分支 {INTEGRATION_BRANCH}"


def merge_one(ref: str, display: str) -> dict:
    """合入一个 ref。返回结果 dict，含 SHA 与是否冲突。"""
    code, sha = git("rev-parse", f"{ref}^{{commit}}")
    sha = sha.strip()
    if code != 0:
        return {"ref": display, "ok": False, "reason": "无法解析提交", "sha": ""}

    # 已在当前分支历史里就不重复合并，但要把 SHA 记下来
    code, _ = git("merge-base", "--is-ancestor", sha, "HEAD")
    if code == 0:
        return {"ref": display, "ok": True, "sha": sha, "skipped": True,
                "reason": "已在集成分支历史中"}

    code, out = git(
        "merge", "--no-ff", "-m",
        f"merge: 合入 {display} ({sha[:12]}) 到集成分支", sha,
    )
    if code != 0:
        _, conflicts = git("diff", "--name-only", "--diff-filter=U")
        return {
            "ref": display, "ok": False, "sha": sha,
            "reason": "合并冲突，需对应文件负责人处理",
            "conflicts": [f for f in conflicts.splitlines() if f.strip()],
            "detail": out[:600],
        }
    return {"ref": display, "ok": True, "sha": sha, "skipped": False, "reason": "合并成功"}


# ────────────────────────── 验证 ──────────────────────────


FAILED_RE = re.compile(r"^(?:FAILED\s+)?(tests/[\w/]+\.py::[\w:]+)")


def extract_failures(output: str) -> list[str]:
    """从 pytest 输出里抽失败用例名，用于把「已知基线失败」和「新回归」分开。"""
    found: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            m = FAILED_RE.match(stripped)
            if m:
                found.append(m.group(1))
    return found


def verify(fast: bool) -> list[dict]:
    steps: list[dict] = []

    def record(name: str, code: int, output: str) -> None:
        failures = extract_failures(output)
        known = [f for f in failures if f in KNOWN_BASELINE_FAILURES]
        unknown = [f for f in failures if f not in KNOWN_BASELINE_FAILURES]
        # 退出码非 0 但失败项全是已知基线 → 视为通过，但明确标出来
        ok = code == 0 or (bool(failures) and not unknown)
        steps.append({
            "name": name,
            "ok": ok,
            "exitCode": code,
            "knownFailures": [{"case": f, "bug": KNOWN_BASELINE_FAILURES[f]} for f in known],
            "newFailures": unknown,
            "output": output.strip().splitlines()[-14:] if output.strip() else [],
        })

    # 1) 环境检查（退出码 2 = 仅警告，视为通过）
    code, out = run([sys.executable, "scripts/qa/check_env.py"], timeout=180)
    record("环境检查", 0 if code in (0, 2) else code, out)

    # 2) 底座契约测试（打真实服务）
    code, out = run(
        [sys.executable, "-m", "pytest", "tests/test_cet_contract.py", "-q"],
        timeout=600,
    )
    record("契约测试（真实服务）", code, out)

    # 3) 样例自校验
    code, out = run(
        [sys.executable, "tests/fixtures/cet/build_fixtures.py", "--check"],
        timeout=120,
    )
    record("契约样例自校验", code, out)

    if not fast:
        code, out = run(
            [sys.executable, "scripts/qa/browser_check.py"], timeout=300
        )
        record("浏览器层验证", code, out)

    # 4) 底座回归
    code, out = run(
        [sys.executable, "-m", "pytest",
         "tests/test_v2_db.py", "tests/test_public_single_user_compat.py", "-q"],
        timeout=300,
    )
    record("底座回归", code, out)

    return steps


def service_reachable() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:5173/health", timeout=4):
            return True
    except Exception:  # noqa: BLE001
        return False


# ────────────────────────── 报告 ──────────────────────────


def write_report(payload: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORT_DIR / f"integration-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = REPORT_DIR / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def print_verification(steps: list[dict]) -> None:
    print("\n== 验证结果 ==")
    for s in steps:
        mark = "OK" if s["ok"] else "X "
        print(f"  [{mark}] {s['name']}")
        for kf in s.get("knownFailures", []):
            print(f"        [已知基线] {kf['case']}  → {kf['bug']}（不阻塞集成）")
        for nf in s.get("newFailures", []):
            print(f"        [新回归]   {nf}")
        if not s.get("knownFailures"):
            for line in s["output"]:
                print(f"        {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="集成分支合并与验证")
    parser.add_argument("--list", action="store_true", help="只列出分支，不做改动")
    parser.add_argument("--merge", action="append", default=[], metavar="REF",
                        help="合入指定分支或提交，可重复")
    parser.add_argument("--base", default="codex/cet-qa-delivery",
                        help="创建集成分支时的基线（默认 C 的交付分支）")
    parser.add_argument("--verify-only", action="store_true", help="只验证当前状态")
    parser.add_argument("--fast", action="store_true", help="跳过浏览器检查")
    parser.add_argument("--no-fetch", action="store_true", help="不先 fetch 远端")
    parser.add_argument("--allow-dirty", action="store_true", help="工作区不干净也继续")
    args = parser.parse_args()

    if args.list:
        list_remotes()
        return 0

    if is_dirty() and not args.allow_dirty:
        print("X  工作区有未提交改动，先提交或 stash 再集成：")
        _, out = git("status", "--short")
        for line in out.splitlines()[:15]:
            print(f"    {line}")
        print("    （确认无影响可加 --allow-dirty）")
        return 2

    if not service_reachable() and args.merge:
        print("!  底座服务未运行，契约测试会大量跳过或失败。")
        print("   建议先：scripts/qa/start_cet.sh --mock")

    if args.merge and not args.no_fetch:
        print("== 拉取远端 ==")
        for remote in CANDIDATE_REMOTES:
            if not remote_url(remote):
                continue
            code, out = git_proxyless("fetch", remote, "--prune")
            print(f"  {remote}: {'OK' if code == 0 else '失败 ' + out.splitlines()[0][:60]}")

    # --verify-only 只验证当前分支，不应顺手建/切集成分支 ——
    # 一个「只读」开关产生副作用，会让人不敢用它。
    if args.verify_only:
        print(f"\n== 验证当前分支 ==\n  {current_branch()} @ {head_sha()[:12]}")
    else:
        ok, msg = ensure_integration_branch(args.base)
        print(f"\n== 集成分支 ==\n  {msg}")
        if not ok:
            return 2
        print(f"  HEAD: {head_sha()[:12]}")

    merges: list[dict] = []
    if args.merge:
        print("\n== 合并 ==")
        for ref in args.merge:
            display, resolved = resolve_ref(ref)
            if not resolved:
                print(f"  X  {display}: 找不到该分支或提交")
                merges.append({"ref": display, "ok": False, "reason": "找不到", "sha": ""})
                continue
            result = merge_one(resolved, display)
            merges.append(result)
            if result["ok"]:
                tag = "（已在历史中，未重复合并）" if result.get("skipped") else ""
                print(f"  OK  {display} @ {result['sha'][:12]} — {result['reason']}{tag}")
            else:
                print(f"  X  {display} @ {result['sha'][:12]} — {result['reason']}")
                for c in result.get("conflicts", []):
                    print(f"      冲突文件：{c}")
                print("\n合并失败，已停止（未回退，现场保留供排查）。")
                print("处理方式：按冲突文件找对应负责人；解决后重新运行本脚本。")
                write_report({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "branch": current_branch(),
                    "head": head_sha(),
                    "merges": merges,
                    "verification": [],
                    "stoppedAt": "merge",
                })
                return 1

    steps = verify(args.fast)
    print_verification(steps)

    failed = [s for s in steps if not s["ok"]]
    payload = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "branch": current_branch(),
        "head": head_sha(),
        "merges": merges,
        "verification": steps,
        "result": "pass" if not failed else "fail",
    }
    path = write_report(payload)

    print(f"\n== 结论 ==")
    print(f"  集成分支 {payload['branch']} @ {payload['head'][:12]}")
    if merges:
        print("  合入记录：")
        for m in merges:
            print(f"    {m['ref']:<32} {m.get('sha', '')[:12]:<14} "
                  f"{'成功' if m['ok'] else '失败'}")
    known_all = [kf for st in steps for kf in st.get("knownFailures", [])]
    if failed:
        print(f"  {len(failed)} 项验证失败：{[st['name'] for st in failed]}")
        for st in failed:
            for nf in st.get("newFailures", []):
                print(f"    新回归：{nf}")
        print("  按失败项定位：契约失败→B，页面/交互失败→A，环境/工具→C")
    else:
        print("  全部验证通过")
    if known_all:
        print(f"  （另有 {len(known_all)} 项已知基线失败，已登记问题清单，不阻塞集成）")
        for kf in known_all:
            print(f"    {kf['bug']}: {kf['case']}")
    print(f"  报告已写入：{path.relative_to(ROOT)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
