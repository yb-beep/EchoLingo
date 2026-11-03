#!/usr/bin/env python3
"""四六级听力黑匣子 · 启动与环境检查（C 负责）。

在启动任何服务之前先跑这个脚本，把「环境问题」和「业务问题」分开。
任务书 6.1「D1 第一步 · 4」和 T01 要求：先把能重复启动的环境确认下来，
再谈页面和接口。D3 换另一台电脑时，这个脚本就是第一道验收。

检查项：
    1. 运行环境    Python 版本、虚拟环境、仓库路径
    2. 外部依赖    FFmpeg / ffprobe 是否存在且可执行
    3. 仓库状态    当前分支、提交号、未提交文件
    4. 数据目录    隔离数据目录是否可写、数据库是否可打开
    5. 共同音频    样例音频是否存在、SHA-256 是否与契约样例一致
    6. 契约样例    tests/fixtures/cet 是否完整且自校验通过
    7. 端口占用    目标端口是否已被占用
    8. 底座服务    可选：服务是否在跑、/health 关键项是否正常

用法：
    .venv/bin/python scripts/qa/check_env.py
    .venv/bin/python scripts/qa/check_env.py --skip-service     # 不探测运行中的服务
    .venv/bin/python scripts/qa/check_env.py --json             # 机器可读输出

退出码：0 全部通过｜1 有阻断项（必须修）｜2 仅有警告（可继续，但要记录）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "cet"
DATA_DIR = ROOT / ".cet-qa" / "data"
MEDIA_DIR = ROOT / ".cet-qa" / "media"
AUDIO_NAME = "cet6-202512-conv2.mp3"
EXPECTED_AUDIO_SHA256 = "de98e7de987d400cb05fe957f21b5a4fa57112df9676bdb4c3f6214ff8a34259"
MIN_PYTHON = (3, 11)
DEFAULT_PORT = int(os.environ.get("ELT_PORT", "5173"))
MOCK_PORT = int(os.environ.get("CET_MOCK_PORT", "5199"))

results: list[dict] = []


def record(ok: bool, level: str, item: str, detail: str, hint: str = "") -> None:
    results.append({"ok": ok, "level": level, "item": item, "detail": detail, "hint": hint})


def check_python() -> None:
    ver = sys.version_info
    ok = (ver.major, ver.minor) >= MIN_PYTHON
    record(
        ok,
        "block" if not ok else "info",
        "Python 版本",
        f"{ver.major}.{ver.minor}.{ver.micro}（{sys.executable}）",
        "" if ok else f"底座要求 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，请用 python3.11 建 venv",
    )
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    record(
        in_venv,
        "warn" if not in_venv else "info",
        "虚拟环境",
        "在 venv 内运行" if in_venv else "未在 venv 内运行（可能污染系统 Python）",
        "" if in_venv else "先 source .venv/bin/activate 或直接用 .venv/bin/python 运行",
    )
    record(True, "info", "操作系统", f"{platform.system()} {platform.release()} {platform.machine()}")


def check_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        path = shutil.which(exe)
        if not path:
            record(
                False,
                "block",
                f"{exe} 可执行文件",
                "未在 PATH 中找到",
                "安装 FFmpeg（macOS: brew install ffmpeg），或用 .env 的 FFMPEG_PATH 指定完整路径",
            )
            continue
        try:
            out = subprocess.run(
                [path, "-version"], capture_output=True, text=True, timeout=15
            ).stdout.splitlines()
            ver = out[0] if out else "(无版本输出)"
            record(True, "info", f"{exe} 可执行文件", f"{path} — {ver[:70]}")
        except Exception as exc:  # noqa: BLE001
            record(False, "block", f"{exe} 可执行文件", f"{path} 无法执行：{exc}",
                   "检查可执行权限或改 FFMPEG_PATH")


def check_repo() -> None:
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        n_dirty = len(dirty.splitlines()) if dirty else 0
        record(True, "info", "仓库分支与提交", f"{branch} @ {sha[:12]}")
        record(
            n_dirty == 0,
            "warn" if n_dirty else "info",
            "未提交文件",
            f"{n_dirty} 个" if n_dirty else "工作区干净",
            "记录实际运行的提交号；有未提交改动时测试结果要对应该状态" if n_dirty else "",
        )
    except Exception as exc:  # noqa: BLE001
        record(False, "warn", "仓库状态", f"无法读取 git 状态：{exc}", "确认当前目录是 Git 工作副本")


def check_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probe = DATA_DIR / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record(True, "info", "数据目录可写", str(DATA_DIR))
    except OSError as exc:
        record(False, "block", "数据目录可写", f"{DATA_DIR} 不可写：{exc}",
               "检查目录权限，或换 ELT_USER_DB 指向的位置")
        return

    db_path = DATA_DIR / "vocab.db"
    if not db_path.exists():
        record(True, "info", "数据库文件", f"{db_path} 尚未创建（首次启动会建）")
        return
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            conn.close()
        ok = bool(quick) and quick[0] == "ok"
        record(ok, "block" if not ok else "info", "数据库可打开",
               f"{db_path.name}：quick_check={quick[0] if quick else '?'}，{tables} 张表")
    except Exception as exc:  # noqa: BLE001
        record(False, "block", "数据库可打开", f"{db_path} 打开失败：{exc}",
               "确认文件未被其它进程锁住；必要时删除该库重建成干净数据")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_audio() -> None:
    candidates = []
    env_path = os.environ.get("CET_QA_MEDIA")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(MEDIA_DIR / AUDIO_NAME)

    found = next((p for p in candidates if p.is_file()), None)
    if found is None:
        record(
            False,
            "block",
            "共同联调音频",
            f"未找到 {AUDIO_NAME}",
            "跑 .venv/bin/python scripts/qa/prepare_audio.py 查找并核对；"
            f"或把音频放到 {MEDIA_DIR / AUDIO_NAME}，或设置 CET_QA_MEDIA 指向它。"
            "音频按仓库 .gitignore 不入库，靠本地路径映射 + SHA-256 核对。",
        )
        return
    digest = sha256_of(found)
    ok = digest == EXPECTED_AUDIO_SHA256
    record(
        ok,
        "block" if not ok else "info",
        "共同联调音频身份",
        f"{found.name} sha256={digest[:16]}… ({found.stat().st_size:,} 字节)",
        ""
        if ok
        else f"哈希与契约样例不一致！期望 {EXPECTED_AUDIO_SHA256[:16]}…。"
        "三人必须用同一段音频，否则时间轴对不上。",
    )
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(found)],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            record(True, "info", "共同联调音频时长", f"{out} 秒")
        except Exception:  # noqa: BLE001
            pass


def check_fixtures() -> None:
    required = [
        "materials.json", "attempt_result.json", "marks.json", "practice.json",
        "practice_steps.json", "review_session.json", "questions_before_attempt.json",
        "transfer_candidates.json", "manifest.json", "build_fixtures.py",
    ]
    missing = [n for n in required if not (FIXTURES / n).exists()]
    if missing:
        record(False, "block", "契约样例完整", f"缺少：{', '.join(missing)}",
               "运行 .venv/bin/python tests/fixtures/cet/build_fixtures.py 重新生成")
        return
    record(True, "info", "契约样例完整", f"{len(required)} 个必备文件齐全")

    try:
        res = subprocess.run(
            [sys.executable, str(FIXTURES / "build_fixtures.py"), "--check"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        ok = res.returncode == 0
        record(ok, "block" if not ok else "info", "契约样例自校验",
               (res.stdout or res.stderr).strip().splitlines()[-1] if (res.stdout or res.stderr) else "",
               "" if ok else "样例与生成脚本不一致，重新运行 build_fixtures.py")
    except Exception as exc:  # noqa: BLE001
        record(False, "warn", "契约样例自校验", f"无法运行校验：{exc}")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def check_ports() -> None:
    for label, port in (("底座服务端口", DEFAULT_PORT), ("Mock API 端口", MOCK_PORT)):
        used = port_in_use(port)
        record(
            True,
            "info" if used else "info",
            label,
            f"{port} 已被占用（服务在跑）" if used else f"{port} 空闲",
        )


def http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def check_service(skip: bool) -> None:
    if skip:
        record(True, "info", "底座服务探测", "已按参数跳过")
        return
    base = f"http://127.0.0.1:{DEFAULT_PORT}"
    try:
        status, health = http_json(f"{base}/health")
    except (urllib.error.URLError, OSError) as exc:
        record(True, "info", "底座服务探测", f"{base} 未运行（{type(exc).__name__}）",
               "需要时用 scripts/qa/start_cet.sh 启动")
        return
    record(True, "info", "底座服务探测", f"{base}/health -> {status}")
    env = health.get("environment", {})

    # 关键：报告的是「这个端口上跑的那个服务」，不一定是本目录的服务。
    # 一台机器同时有多份副本时（比如验收另一个目录），不核对归属会得出
    # 「我的环境没问题」的错误结论 —— 实际查的是别人的库。
    svc_py = str((env.get("python") or {}).get("executable") or "")
    svc_db = str((env.get("sqlite") or {}).get("path") or "")
    belongs = svc_py.startswith(str(ROOT)) or svc_db.startswith(str(ROOT))
    if not belongs and (svc_py or svc_db):
        record(
            False,
            "warn",
            "  服务归属",
            f"端口 {DEFAULT_PORT} 上运行的不是本目录的服务",
            f"本目录 {ROOT}；该服务来自 {svc_db or svc_py}。"
            "下面的服务侧数据属于另一个副本，不能代表本目录环境。"
            "换目录验收时先停掉旧服务，或换端口：PORT=5174 scripts/qa/start_cet.sh --mock",
        )
    else:
        record(True, "info", "  服务归属", "该服务来自本目录")
    py = env.get("python", {})
    record(True, "info", "  服务侧 Python", py.get("version", "?").split(" ")[0])
    record(bool(env.get("ffmpeg", {}).get("available")), "block", "  服务侧 FFmpeg",
           str(env.get("ffmpeg", {}).get("path")), "服务看不到 FFmpeg，转写与切句会失败")
    sqlite_info = env.get("sqlite", {})
    ok = sqlite_info.get("status") == "ok"
    record(ok, "block" if not ok else "info", "  服务侧数据库",
           f"{sqlite_info.get('path')} status={sqlite_info.get('status')} "
           f"quick_check={sqlite_info.get('quick_check')}",
           "" if ok else "服务侧数据库异常，先修数据目录再谈业务")
    keys = env.get("keys", {})
    ai_missing = keys.get("AI_API_KEY") == "missing"
    record(
        not ai_missing,
        "warn" if ai_missing else "info",
        "  AI 密钥配置",
        "AI_API_KEY=missing（模型功能不可用）" if ai_missing else "AI_API_KEY=已配置",
        "无密钥时 AI 接口会报 401/500；带练验收需改测本地动作或 Mock。"
        "交接记录只写是否配置成功，不写密钥内容。" if ai_missing else "",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="EchoLingo 四六级改造 · 启动与环境检查")
    parser.add_argument("--skip-service", action="store_true", help="不探测运行中的服务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    check_python()
    check_ffmpeg()
    check_repo()
    check_data_dir()
    check_audio()
    check_fixtures()
    check_ports()
    check_service(args.skip_service)

    blocks = [r for r in results if not r["ok"] and r["level"] == "block"]
    warns = [r for r in results if not r["ok"] and r["level"] == "warn"]

    if args.json:
        print(json.dumps(
            {"ok": not blocks, "blockers": len(blocks), "warnings": len(warns), "results": results},
            ensure_ascii=False, indent=2,
        ))
    else:
        icon = {"info": "  ", "warn": "! ", "block": "X "}
        print("\n四六级听力黑匣子 · 启动与环境检查")
        print(f"仓库：{ROOT}\n" + "-" * 72)
        for r in results:
            mark = "OK" if r["ok"] else icon.get(r["level"], "? ")
            print(f"[{mark}] {r['item']}: {r['detail']}")
            if r["hint"] and not r["ok"]:
                print(f"       → {r['hint']}")
        print("-" * 72)
        if blocks:
            print(f"结论：{len(blocks)} 个阻断项必须先修，{len(warns)} 个警告可记录后继续")
            for b in blocks:
                print(f"  阻断：{b['item']} — {b['detail']}")
        elif warns:
            print(f"结论：无阻断项，{len(warns)} 个警告（记录后继续）")
        else:
            print("结论：全部检查通过")

    if blocks:
        return 1
    return 2 if warns else 0


if __name__ == "__main__":
    sys.exit(main())
