#!/usr/bin/env python3
"""四六级听力黑匣子 · 交付包自洽性检查（C 负责）。

为什么需要它：
    C 维护 11 份 QA 文档、16 个脚本、10 个样例。内容一多就会出现
    「文档说 56 项、实际 81 项」这类矛盾 —— 对内部人只是困惑，
    对**拿到交付包的外部读者**就是误导：他会以为某项功能不存在，或按旧数字判断进度。

    这个脚本把「交付包是否自洽」变成可执行检查，而不是靠人记得同步。

检查六类：
    1. 文档内部链接是否断链
    2. 文档引用的脚本/样例文件是否存在
    3. 文档里的检查命令是否真能跑（且退出码符合文档描述）
    4. 各文档声称的测试数量是否与实际一致
    5. 文档引用的接口清单是否与接口规格一致
    6. 敏感信息（本机绝对路径）是否泄露进交付文档

用法：
    .venv/bin/python scripts/qa/check_package_consistency.py
    .venv/bin/python scripts/qa/check_package_consistency.py --json
    .venv/bin/python scripts/qa/check_package_consistency.py --skip-run   # 不跑命令（快）

退出码：0 自洽｜1 有不一致｜2 环境不满足
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "qa"
SPEC = DOCS / "interface-spec.json"

results: list[dict] = []


def record(level: str, item: str, detail: str, hint: str = "") -> None:
    results.append({"level": level, "item": item, "detail": detail, "hint": hint})


def docs() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


# ────────────────────────── 1. 内部链接 ──────────────────────────


def check_links() -> None:
    broken: list[str] = []
    total = 0
    for d in docs():
        text = d.read_text(encoding="utf-8")
        for m in re.finditer(r"\[[^\]]+\]\((\.{1,2}/[^)]+)\)", text):
            total += 1
            target = (d.parent / m.group(1)).resolve()
            if not target.exists():
                broken.append(f"{d.name} → {m.group(1)}")
    if broken:
        record("block", "文档内部链接", f"{len(broken)} 处断链")
        for b in broken[:8]:
            record("block", "  断链", b)
    else:
        record("ok", "文档内部链接", f"{len(docs())} 份文档、{total} 条内部链接，无断链")


# ────────────────────────── 2. 引用的文件存在 ──────────────────────────

REF_PAT = re.compile(r"(scripts/qa/[\w\-.]+\.(?:py|sh)|tests/[\w/\-.]+\.(?:py|json))")


def check_referenced_files() -> None:
    refs: set[str] = set()
    for d in docs():
        for m in REF_PAT.finditer(d.read_text(encoding="utf-8")):
            refs.add(m.group(1))
    missing = [r for r in sorted(refs) if not (ROOT / r).exists()]
    if missing:
        record("block", "文档引用的文件", f"{len(missing)} 个不存在")
        for m in missing[:8]:
            record("block", "  缺失", m, "补上文件或修正文档引用")
    else:
        record("ok", "文档引用的文件", f"引用 {len(refs)} 个脚本/样例，全部存在")


# ────────────────────────── 3. 检查命令可跑 ──────────────────────────


def check_commands() -> None:
    """跑文档里列出的检查命令，确认退出码在「预期集合」内。"""
    # (命令, 允许的退出码, 说明)
    cases = [
        ([sys.executable, "scripts/qa/check_env.py"], {0, 2}, "环境检查（2=仅警告）"),
        ([sys.executable, "scripts/qa/prepare_audio.py"], {0}, "音频就位"),
        ([sys.executable, "scripts/qa/gen_api_spec.py", "--check"], {0}, "接口规格一致"),
        ([sys.executable, "scripts/qa/verify_answer_provenance.py"], {0}, "答案来源核对"),
        ([sys.executable, "scripts/qa/audit_delivery.py"], {0, 1}, "合规审计"),
        ([sys.executable, "tests/fixtures/cet/build_fixtures.py", "--check"], {0}, "样例自校验"),
    ]
    bad = []
    for cmd, allowed, label in cases:
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            if r.returncode not in allowed:
                bad.append(f"{label}：退出码 {r.returncode}（预期 {sorted(allowed)}）")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{label}：执行失败 {exc}")
    if bad:
        record("block", "文档检查命令", f"{len(bad)} 条不符合预期")
        for b in bad:
            record("block", "  命令", b)
    else:
        record("ok", "文档检查命令", f"{len(cases)} 条命令退出码均符合文档描述")


# ────────────────────────── 4. 测试数量一致 ──────────────────────────


def actual_test_count() -> int | None:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_cet_contract.py",
             "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        for line in reversed(r.stdout.splitlines()):
            m = re.search(r"(\d+)\s+tests? collected", line)
            if m:
                return int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None


COUNT_PATTERNS = [
    re.compile(r"\*\*(\d+)\s*passed\*\*"),
    re.compile(r"`tests/test_cet_contract\.py`\s*\*\*(\d+)\s*项\*\*"),
    re.compile(r"合计\s*\*\*(\d+)\s*项\*\*"),
    re.compile(r"\| 契约测试 \|\s*(\d+)\s*passed\s*\|"),
    re.compile(r"→\s*(\d+)\s*passed"),
    re.compile(r"Mock API\s*(\d+)\s*项全绿"),
]


def check_counts() -> None:
    actual = actual_test_count()
    if actual is None:
        record("warn", "测试数量一致", "无法统计实际用例数（pytest collect 失败）")
        return

    found: dict[str, list[int]] = {}
    for d in docs():
        text = d.read_text(encoding="utf-8")
        for pat in COUNT_PATTERNS:
            for m in pat.finditer(text):
                n = int(m.group(1))
                # 只关心「测试总数」量级的数字，避免把 4 项并发误判
                if 20 <= n <= 500:
                    found.setdefault(d.name, []).append(n)

    offenders = {
        name: sorted(set(nums))
        for name, nums in found.items()
        if any(n != actual for n in nums)
    }
    if offenders:
        record("block", "测试数量一致", f"实际 {actual} 项，以下文档数字不符")
        for name, nums in offenders.items():
            record("block", "  数字不符", f"{name}：{nums}（应为 {actual}）",
                   "统一改为实际数量，或删除易过期的具体数字")
    else:
        record("ok", "测试数量一致", f"各文档声称的数量与实际 {actual} 项一致")


# ────────────────────────── 5. 接口清单一致 ──────────────────────────


def check_endpoints() -> None:
    if not SPEC.exists():
        record("warn", "接口清单一致", "未找到 interface-spec.json，跳过")
        return
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    def _norm(path: str) -> str:
        """把各种占位/具体写法归一成同一形式再比较。

        规格里长这样：  /api/cet/practices/practice-<id>/ai-cancel
        文档里长这样：  /api/cet/practices/{id}/ai-cancel
        不归一化会把同一接口当成两个，产生一堆误报；
        但归一过头（把 practices 这类段名也吃掉）又会让所有接口撞成同一个。
        所以只替换「段名 + ID」中的 ID 部分。
        """
        p = path.split("?")[0]
        p = re.sub(
            r"/(practices|materials|marks|reviews|attempts)"
            r"/(?:\{[^}]+\}|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)",
            r"/\1/{id}", p,
        )
        p = re.sub(r"-?<id>", "", p)          # 清残留 -<id>
        p = re.sub(r"/{id}/{id}", "/{id}", p)
        return p

    spec_paths = {_norm(e["path"]) for e in spec.get("endpoints", [])}
    # 文档里提到的 CET 接口
    doc_paths: set[str] = set()
    for d in docs():
        for m in re.finditer(r"(/api/cet/[\w/\-{}<>]+)", d.read_text(encoding="utf-8")):
            p = m.group(1).rstrip(".,;)")
            doc_paths.add(_norm(p))
    # 排除不属于业务契约的路径：
    #   _mock/*      Mock 控制面，本来就不在业务规格里
    #   {id} 截断形 文档里写 mark- / practice- 这类示例前缀，不是真实路径
    def _is_business(p: str) -> bool:
        if "/_mock/" in p:
            return False
        if p.endswith("/anything"):      # 文档里用来说明「命名空间干净」的反例
            return False
        # 末尾是 xxx- 说明是截断的示例 ID
        if re.search(r"-[^/]*$", p) and not p.endswith("{id}"):
            tail = p.rsplit("/", 1)[-1]
            if tail.endswith("-"):
                return False
        return True

    unknown = sorted(p for p in doc_paths if p not in spec_paths and _is_business(p))
    if unknown:
        record("warn", "接口清单一致",
               f"文档提到 {len(unknown)} 个接口不在规格里（可能是示例路径）")
        for u in unknown[:6]:
            record("warn", "  未收录", u)
    else:
        record("ok", "接口清单一致",
               f"文档提到的 {len(doc_paths)} 个接口都在规格的 {len(spec_paths)} 条里")


# ────────────────────────── 6. 敏感路径泄露 ──────────────────────────

# 交付文档里不该出现本机绝对路径（会暴露用户名/目录结构）
ABS_PATH_PAT = re.compile(r"/Users/[A-Za-z0-9_.\-]+/")
# 有意保留的白名单：说明「音频来源在哪」需要路径，属于必要的可追溯信息
ALLOWED_CONTEXT = ("旧项目", "originPath", "desktop/.acceptance", "prepare_audio",
                   # 本机工作副本路径：对内部交接有用（照抄即可跑），
                   # 对外发布前应替换。这里不拦，只统计。
                   )


def check_leaks() -> None:
    hits: list[str] = []
    for d in docs():
        for i, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
            if ABS_PATH_PAT.search(line):
                if any(a in line for a in ALLOWED_CONTEXT):
                    continue
                hits.append(f"{d.name}:{i}  {line.strip()[:80]}")
    if hits:
        record("warn", "本机路径泄露", f"{len(hits)} 处（交付给外部时应改为相对路径）")
        for h in hits[:6]:
            record("warn", "  路径", h)
    else:
        record("ok", "本机路径泄露", "交付文档未泄露本机绝对路径")


def check_self_contained() -> None:
    """交付包必须能独立跑：工具不能把「不在 git 里的本机文件」当作唯一数据来源。

    这条是因为真踩过：verify_answer_provenance.py 原先只读 C 本机一个绝对路径的
    答案解析文本，而该文件不在 git 里 —— 外部机器上任务书 11 节要求的
    「答案来源核对」必然失败。

    判定原则：
    - 允许把本机路径作为**候选之一**（如 prepare_audio 的音频，受版权限制本就不能入库）
    - 不允许「唯一来源是本机路径」而没有 git 内的替代
    """
    offenders: list[str] = []
    for f in sorted((ROOT / "scripts" / "qa").glob("*.py")):
        text = f.read_text(encoding="utf-8", errors="replace")
        if "/Users/" not in text:
            continue
        # 是否同时引用了仓库内的 fixtures（说明有自包含替代）
        has_bundled = ("tests/fixtures" in text or "FIXTURES" in text
                       or "ROOT /" in text)
        # 是否明确把本机路径标为「候选/回退」
        marked_fallback = any(k in text for k in ("候选", "回退", "若存在", "fallback"))
        if not (has_bundled or marked_fallback):
            offenders.append(f.name)

    if offenders:
        record("warn", "交付包自包含",
               f"{len(offenders)} 个脚本只依赖本机路径且无替代：{offenders}",
               "把必要数据固化进 tests/fixtures，或把本机路径标为候选或回退")
    else:
        record("ok", "交付包自包含",
               "含本机路径的脚本都有仓库内替代或明确标为回退")


def main() -> int:
    parser = argparse.ArgumentParser(description="交付包自洽性检查")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-run", action="store_true", help="不跑检查命令（快）")
    args = parser.parse_args()

    check_links()
    check_referenced_files()
    if not args.skip_run:
        check_commands()
    check_counts()
    check_endpoints()
    check_leaks()
    check_self_contained()

    blocks = [r for r in results if r["level"] == "block"]
    warns = [r for r in results if r["level"] == "warn"]

    if args.json:
        print(json.dumps({"blockers": len(blocks), "warnings": len(warns),
                          "results": results}, ensure_ascii=False, indent=2))
        return 0 if not blocks else 1

    print("四六级听力黑匣子 · 交付包自洽性检查")
    print(f"文档目录 {DOCS}")
    print("-" * 72)
    for r in results:
        mark = {"ok": "OK", "warn": "! ", "block": "X "}[r["level"]]
        print(f"[{mark}] {r['item']}: {r['detail']}")
        if r["hint"]:
            print(f"       → {r['hint']}")
    print("-" * 72)
    if blocks:
        print(f"结论：{len(blocks)} 个阻断项 —— 交付包不自洽，先修")
        return 1
    print(f"结论：无阻断项" + (f"，{len(warns)} 项建议处理" if warns else "，交付包自洽"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
