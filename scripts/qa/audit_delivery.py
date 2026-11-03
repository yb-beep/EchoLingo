#!/usr/bin/env python3
"""四六级听力黑匣子 · 交付合规审计（C 负责）。

读懂这份脚本在防什么，比跑它更重要：
    前三轮 C 一直在查「代码对不对」，这个脚本查的是「能不能交出去」。
    代码全绿但许可有问题、或演示包里混进了密钥，一样交付失败。

任务书第 11 节逐条要求，这里逐条落实为可执行检查：
    1. 密钥通过本地配置读取，源码/样例/日志/PPT/演示包排除密钥
    2. 演示默认限定本机访问
    3. 分发时附许可或链接及要求的版权声明
    4. 材料使用范围（音频不入库、来源可追溯）
    5. 视频与演示区分真实输出 / 缓存结果 / 模拟场景

**最重要的一条发现（见 §licenseRisk）**：
    EchoLingo 用 PolyForm Noncommercial 1.0.0。该许可允许
    「个人研究、实验、学习、爱好」与「教育机构、公益组织」等非商业用途。
    参加商业性比赛、或做成产品对外提供服务，**是否落在许可范围内必须由人确认**。
    这不是 C 能拍板的，但 C 有责任在交付前把它明确摆出来，而不是等提交后被追问。

用法：
    .venv/bin/python scripts/qa/audit_delivery.py
    .venv/bin/python scripts/qa/audit_delivery.py --json
    .venv/bin/python scripts/qa/audit_delivery.py --include-git-history   # 扫全部提交

退出码：0 无阻断｜1 有阻断项（不可交付）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 密钥形态。宁可多报，让人看一眼确认，也不要漏。
SECRET_PATTERNS = [
    ("OpenAI/DeepSeek 风格", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("Groq", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("DashScope", re.compile(r"\bsk-[a-f0-9]{32}\b")),
    ("通用 Bearer", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{24,}")),
    ("AWS AK", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# 允许出现的占位写法（示例配置、提示文本）
PLACEHOLDER_HINTS = (
    "your_key", "your-key", "yourkey", "missing-api-key", "sk-xxx",
    "sk-your", "<your", "example", "placeholder", "xxx", "***", "…",
)

# 扫描范围：只扫会进交付物的文本文件
SCAN_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".css", ".json",
    ".md", ".sh", ".ini", ".txt", ".yml", ".yaml", ".env", ".example",
}
# 这些目录不扫（依赖、缓存、虚拟环境、运行数据）
SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".cet-qa",
    ".cache", ".pytest_cache", "dist", "build", ".next",
}

results: list[dict] = []


def record(level: str, item: str, detail: str, hint: str = "") -> None:
    results.append({"level": level, "item": item, "detail": detail, "hint": hint})


def iter_files() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SCAN_SUFFIXES or p.name in (".env.example",):
            out.append(p)
    return out


# ────────────────────────── 1. 密钥扫描 ──────────────────────────


def scan_secrets(files: list[Path]) -> None:
    hits: list[tuple[str, str, int, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                snippet = m.group(0)
                low = snippet.lower()
                if any(h in low for h in PLACEHOLDER_HINTS):
                    continue
                line_no = text[: m.start()].count("\n") + 1
                hits.append((label, str(f.relative_to(ROOT)), line_no, snippet[:14] + "…"))

    if hits:
        record("block", "密钥泄漏", f"发现 {len(hits)} 处疑似密钥")
        for label, path, line, frag in hits[:10]:
            record("block", f"  {label}", f"{path}:{line}  {frag}",
                   "立即从源码移除、轮换该密钥；确认是否已进入 git 历史")
    else:
        record("ok", "密钥扫描", f"扫 {len(files)} 个文件，未发现明文密钥")


def scan_git_history() -> None:
    """扫全部提交历史 —— 密钥删掉了但仍留在历史里，一样会随仓库分发。"""
    try:
        out = subprocess.run(
            ["git", "log", "-p", "--all", "--", "."],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        record("warn", "git 历史扫描", f"无法读取：{exc}")
        return

    hits = 0
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(out):
            low = m.group(0).lower()
            if any(h in low for h in PLACEHOLDER_HINTS):
                continue
            hits += 1
            if hits <= 5:
                record("block", "  git 历史中的密钥", f"{label}: {m.group(0)[:14]}…",
                       "历史中的密钥需改写历史或轮换；仅删除当前文件不够")
    if hits == 0:
        record("ok", "git 历史扫描", "全部提交历史未发现明文密钥")
    else:
        record("block", "git 历史密钥", f"共 {hits} 处")


# ────────────────────────── 2. 许可与版权 ──────────────────────────


def check_license() -> None:
    lic = ROOT / "LICENSE"
    if not lic.exists():
        record("block", "LICENSE 文件", "缺失", "上游许可必须随分发保留")
        return
    text = lic.read_text(encoding="utf-8", errors="replace")
    is_polyform = "PolyForm Noncommercial" in text
    record("ok", "LICENSE 文件", "存在" + ("（PolyForm Noncommercial 1.0.0）" if is_polyform else ""))

    # 许可要求的版权声明
    m = re.search(r"Required Notice:\s*(.+)", text)
    if m:
        record("ok", "许可要求的版权声明", m.group(1).strip())
    else:
        record("warn", "许可要求的版权声明", "未找到 'Required Notice' 行，分发前需人工确认")

    third = ROOT / "THIRD_PARTY_LICENSES.md"
    if third.exists():
        record("ok", "第三方许可清单", f"存在（{third.stat().st_size:,} 字节）")
    else:
        record("warn", "第三方许可清单", "缺失 THIRD_PARTY_LICENSES.md")

    if is_polyform:
        # 这是整个审计里最需要人看的一条，单独高亮
        record(
            "warn",
            "许可适用性（需人工确认）",
            "PolyForm Noncommercial 1.0.0：允许个人研究/学习/爱好与教育机构、"
            "公益组织等非商业用途",
            "参加商业性比赛或做成对外产品，是否落在许可范围内必须由用户确认。"
            "该许可要求分发时附带许可文本与版权声明。C 不代拍板，但交付前必须明确。",
        )


def check_attribution(files: list[Path]) -> None:
    """我方新增文件不应声称上游代码的所有权；引用来源要写明。"""
    ours = subprocess.run(
        ["git", "diff", "--name-only", "1ea5b35..HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.splitlines()
    existing = [p for p in ours if (ROOT / p).exists()]
    if not existing:
        record("warn", "新增文件归属", "未找到相对基线的改动，跳过")
        return

    # 检查新增文档是否声明了与上游的关系
    qa_docs = [p for p in existing if p.startswith("docs/qa/") and p.endswith(".md")]
    declared = 0
    for p in qa_docs:
        try:
            if "EchoLingo" in (ROOT / p).read_text(encoding="utf-8", errors="replace"):
                declared += 1
        except OSError:
            pass
    record(
        "ok",
        "新增文件归属",
        f"{len(existing)} 个新增文件；其中 {declared}/{len(qa_docs)} 份 QA 文档提及 EchoLingo 上游",
    )


# ────────────────────────── 3. 材料使用范围 ──────────────────────────


def check_media_scope() -> None:
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
        ).stdout.splitlines()
    except Exception as exc:  # noqa: BLE001
        record("warn", "媒体入库检查", f"无法读取：{exc}")
        return

    media = [t for t in tracked
             if Path(t).suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".mp4", ".webm")]
    if media:
        record("block", "音频/视频入库", f"{len(media)} 个媒体文件被提交：{media[:5]}",
               "音频不应入库（版权与体积）；用本地配置映射 + SHA-256 核对")
    else:
        record("ok", "音频/视频入库", "无媒体文件入库（符合约定）")

    # 数据库与用户数据。
    # 注意：上游有意把内置词典 resources/ecdict/*.db 入库（.gitignore 里
    # 明确用 !resources/ecdict/ecdict.db 白名单放开），那是**发行物**不是用户数据，
    # 不能一刀切按 .db 后缀判违规 —— 否则会误报，让人不敢信这个审计。
    ALLOWED_DB = {
        "resources/ecdict/ecdict.db",
        "resources/ecdict/gloss_compact.db",
    }
    risky = []
    for t in tracked:
        if t in ALLOWED_DB:
            continue
        name = Path(t).name
        if name in ("vocab.db", ".env") or name.endswith(".db"):
            risky.append(t)
    if risky:
        record("block", "运行数据入库", f"{risky[:5]}", "数据库与 .env 永不入库")
    else:
        record("ok", "运行数据入库", "无用户数据库 / .env 入库（内置词典属发行物，已放行）")

    # 材料来源可追溯
    manifest = ROOT / "tests" / "fixtures" / "cet" / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            audio = data.get("audio", {})
            has_sha = bool(audio.get("sha256"))
            has_origin = bool(audio.get("originPath"))
            record("ok", "材料来源可追溯",
                   f"音频 sha256 {'有' if has_sha else '缺'}，来源路径 {'有' if has_origin else '缺'}")
            if not (has_sha and has_origin):
                record("warn", "材料来源", "manifest 缺少哈希或来源路径")
        except Exception as exc:  # noqa: BLE001
            record("warn", "材料来源可追溯", f"manifest 解析失败：{exc}")
    else:
        record("block", "材料清单", "缺少 tests/fixtures/cet/manifest.json")


# ────────────────────────── 4. 本机访问范围 ──────────────────────────


def check_bind_scope() -> None:
    env_example = ROOT / ".env.example"
    script = ROOT / "scripts" / "qa" / "start_cet.sh"
    host_hint = ""
    if env_example.exists():
        for line in env_example.read_text(encoding="utf-8").splitlines():
            if line.startswith("ELT_HOST"):
                host_hint = line.strip()
    if script.exists():
        text = script.read_text(encoding="utf-8")
        if "ELT_HOST:-127.0.0.1" in text or "ELT_HOST=127.0.0.1" in text:
            record("ok", "演示访问范围", "启动脚本默认绑定 127.0.0.1（仅本机）")
        else:
            record("warn", "演示访问范围", "启动脚本未显式绑定 127.0.0.1",
                   "底座默认 0.0.0.0:5173，同网络设备可访问；演示应限定本机")
    else:
        record("warn", "演示访问范围", "未找到启动脚本")


# ────────────────────────── 5. 模拟/真实标注 ──────────────────────────


def check_fixture_labeling() -> None:
    fx = ROOT / "tests" / "fixtures" / "cet"
    if not fx.exists():
        record("warn", "样例标识", "未找到契约样例目录")
        return
    jsons = sorted(fx.glob("*.json"))
    # manifest.json 是清单（记录哈希与文件列表），本身不是样例句，不参与标注统计
    NOT_FIXTURES = {"manifest.json"}
    labeled = 0
    for j in jsons:
        if j.name.endswith(".source.json") or j.name in NOT_FIXTURES:
            continue
        try:
            data = json.loads(j.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("developmentFixture"):
            labeled += 1
    total = len([j for j in jsons
                 if not j.name.endswith(".source.json") and j.name not in NOT_FIXTURES])
    if total and labeled == total:
        record("ok", "样例标识", f"{labeled}/{total} 个样例带 developmentFixture")
    else:
        record("warn", "样例标识", f"仅 {labeled}/{total} 个样例带 developmentFixture",
               "开发样例必须可区分，避免与真实输出混淆")


def main() -> int:
    parser = argparse.ArgumentParser(description="交付合规审计")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-git-history", action="store_true",
                        help="额外扫全部提交历史（较慢）")
    args = parser.parse_args()

    files = iter_files()
    scan_secrets(files)
    if args.include_git_history:
        scan_git_history()
    check_license()
    check_attribution(files)
    check_media_scope()
    check_bind_scope()
    check_fixture_labeling()

    blocks = [r for r in results if r["level"] == "block"]
    warns = [r for r in results if r["level"] == "warn"]

    if args.json:
        print(json.dumps({"blockers": len(blocks), "warnings": len(warns),
                          "results": results}, ensure_ascii=False, indent=2))
        return 0 if not blocks else 1

    print("四六级听力黑匣子 · 交付合规审计")
    print(f"仓库 {ROOT}")
    print("-" * 74)
    for r in results:
        mark = {"ok": "OK", "warn": "! ", "block": "X "}[r["level"]]
        print(f"[{mark}] {r['item']}: {r['detail']}")
        if r["hint"]:
            print(f"       → {r['hint']}")
    print("-" * 74)
    if blocks:
        print(f"结论：{len(blocks)} 个阻断项 —— 当前状态不可交付")
        return 1
    print(f"结论：无阻断项" + (f"，{len(warns)} 项需人工确认" if warns else "，可直接交付"))
    if warns:
        print("\n需人工确认的事项（C 不代拍板）：")
        for w in warns:
            print(f"  · {w['item']}：{w['detail']}")
            if w["hint"]:
                print(f"    {w['hint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
