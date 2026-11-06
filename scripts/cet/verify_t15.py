#!/usr/bin/env python
"""T15 独立验证：从交付说明启动，两份不同级别素材走完整流程。

**这是验收方可以直接复现的版本**：脚本自己起服务、自己建空库、自己导入材料、
跑完自己停掉，不依赖「已经有一个服务在 5189 上跑着」这种隐含前提。

关键纪律（对应任务书 T15「不依赖硬编码 ID/本机测试记录」）：
1. 不硬编码 lesson id —— 全部从 `/api/cet/base/materials` 反查
2. 不硬编码端口 —— `--port` 可换，默认 5189
3. 不依赖本机残留数据 —— 每次新建临时数据目录
4. 不依赖已有服务 —— 自己起、自己停

用法：
    .venv/bin/python scripts/cet/verify_t15.py
    .venv/bin/python scripts/cet/verify_t15.py --port 5193 --keep
    .venv/bin/python scripts/cet/verify_t15.py --out /tmp/t15-evidence

退出码：0 全通过｜1 有失败｜2 环境不满足
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
STEPS: list[dict] = []


def rec(step: str, ok: bool, actual: str, expected: str = "") -> bool:
    STEPS.append({"step": step, "pass": bool(ok), "expected": expected, "actual": actual})
    print(f"  [{'PASS' if ok else 'FAIL'}] {step}: {actual}")
    if not ok and expected:
        print(f"         期望: {expected}")
    return bool(ok)


def http_json(url: str, timeout: float = 15.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5189)
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="cet-t15-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="cet-t15-data-"))
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"

    print("=" * 70)
    print("  T15 从交付说明启动 · 两级别素材完整流程")
    print(f"  端口：{args.port}   数据：{data_dir}")
    print("=" * 70)

    # ── 0. 端口必须空闲（证明是全新启动，不是复用本机残留）──
    if not port_free(args.port):
        print(f"X  端口 {args.port} 已被占用，请换端口：--port 5193")
        return 2
    rec("0a 端口空闲（全新启动）", True, f"{args.port} 空闲")

    env = {**os.environ}
    env.update({
        "ELT_HOST": "127.0.0.1", "ELT_PORT": str(args.port),
        "ELT_USER_DB": str(data_dir / "vocab.db"),
        "ELT_CONFIG_DIR": str(data_dir / "config"),
    })
    for k in ("AI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "AI_BASE_URL"):
        env.pop(k, None)   # 证明无 key 可用

    # ── 1. 空库启动 ──
    log = data_dir / "server.log"
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "backend/fastapi_server.py"],
        cwd=str(ROOT), env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT,
    )
    ok = False
    for _ in range(120):
        if http_json(f"{base}/health", timeout=2)[0] == 200:
            ok = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if not ok:
        print(f"X  服务未就绪，日志：{log}")
        print(log.read_text(errors="replace")[-1200:] if log.exists() else "")
        proc.kill()
        return 2
    rec("1 空库启动成功（无 AI key）", True, f"{base}/health 200")

    try:
        st, mats = http_json(f"{base}/api/cet/base/materials")
        items = (mats or {}).get("items") or []
        rec("2 空库材料列表为空且不报错", st == 200 and items == [],
            f"{st} count={len(items)}", "200 + items=[]")

        # ── 3. 导入材料（按交付说明的命令）──
        imp = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "python"),
             "scripts/cet/import_materials.py", "--all"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        rec("3 按交付说明导入样例材料", imp.returncode == 0,
            (imp.stdout or imp.stderr).strip().splitlines()[-1][:120])

        # ── 4. 反查 ID（不硬编码）──
        st, mats = http_json(f"{base}/api/cet/base/materials")
        items = (mats or {}).get("items") or []
        by_level: dict[str, list] = {}
        for m in items:
            lu = str(m.get("lessonUrl") or "")
            tail = lu.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                by_level.setdefault(m.get("level"), []).append(
                    {"id": int(tail), "title": m.get("title"), "url": lu})
        has_both = bool(by_level.get("cet4")) and bool(by_level.get("cet6"))
        rec("4 接口反查到两份不同级别材料", has_both,
            json.dumps({k: [x["id"] for x in v] for k, v in by_level.items()},
                       ensure_ascii=False),
            "cet4 与 cet6 各至少 1 份")
        if not has_both:
            return 1

        cet4 = by_level["cet4"][0]
        cet6 = by_level["cet6"][0]

        # ── 5. 浏览器完整流程 ──
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("X  Playwright 未安装，无法验证浏览器层")
            return 2

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda e: page_errors.append(str(e)[:100]))

            # 首页
            page.goto(f"{base}/", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            title = page.title()
            rec("5 首页标题为 CET 口径", ("四六级" in title or "听力" in title), title)
            page.screenshot(path=str(out_dir / "t15-01-home.png"))

            # 四级材料
            page.goto(f"{base}{cet4['url']}", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3500)
            probe4 = page.evaluate("""async () => {
                const m = document.querySelector('#local-media-player')
                       || document.querySelector('audio');
                if (!m) return {ok: false, reason: 'no media element'};
                const out = {dur: +(+m.duration).toFixed(1),
                             src: (m.currentSrc || m.src || '').slice(-45)};
                try { await m.play(); } catch (e) { out.playErr = String(e).slice(0, 50); }
                await new Promise(r => setTimeout(r, 800));
                out.played = +(+m.currentTime).toFixed(2);
                m.pause();
                m.currentTime = 30;
                await new Promise(r => setTimeout(r, 300));
                out.seek = +(+m.currentTime).toFixed(1);
                m.playbackRate = 1.5;
                out.rate = m.playbackRate;
                m.playbackRate = 1.0;
                out.ok = out.played > 0.1 && Math.abs(out.seek - 30) < 1.5;
                return out;
            }""")
            rec("6 四级材料真实播放", probe4.get("played", 0) > 0.1,
                f"played={probe4.get('played')}s dur={probe4.get('dur')}s")
            rec("7 四级材料拖动到 30s", abs(float(probe4.get("seek", 0)) - 30) < 1.5,
                f"currentTime={probe4.get('seek')}", "≈30.0")
            rec("8 四级材料倍速 1.5x 生效", probe4.get("rate") == 1.5,
                str(probe4.get("rate")), "1.5")
            rec("9 四级音源正确", "cet4" in str(probe4.get("src", "")),
                str(probe4.get("src")))
            page.screenshot(path=str(out_dir / "t15-02-cet4.png"))

            # 刷新恢复
            page.reload(wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            after = page.evaluate("""() => {
                const m = document.querySelector('#local-media-player')
                       || document.querySelector('audio');
                return m ? {t: +(+m.currentTime).toFixed(1), dur: +(+m.duration).toFixed(1)} : null;
            }""")
            rec("10 刷新后播放器仍可用", after is not None,
                json.dumps(after))

            # 六级材料：切换后音源/时长必须换（不能 A 显示播 B 音频）
            page.goto(f"{base}{cet6['url']}", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3500)
            probe6 = page.evaluate("""async () => {
                const m = document.querySelector('#local-media-player')
                       || document.querySelector('audio');
                if (!m) return {ok: false, reason: 'no media element'};
                m.currentTime = 60;
                await new Promise(r => setTimeout(r, 300));
                return {dur: +(+m.duration).toFixed(1),
                        seek: +(+m.currentTime).toFixed(1),
                        src: (m.currentSrc || m.src || '').slice(-45)};
            }""")
            rec("11 六级材料音源已切换", "cet6" in str(probe6.get("src", "")),
                str(probe6.get("src")))
            rec("12 六级材料时长正确（≠ 四级）",
                abs(float(probe6.get("dur", 0)) - float(probe4.get("dur", 0))) > 1,
                f"cet4={probe4.get('dur')}s cet6={probe6.get('dur')}s",
                "两份时长应不同")
            rec("13 六级材料拖动到 60s", abs(float(probe6.get("seek", 0)) - 60) < 1.5,
                f"currentTime={probe6.get('seek')}", "≈60.0")
            page.screenshot(path=str(out_dir / "t15-03-cet6.png"))

            # 原文渲染
            transcript = page.evaluate("""() => {
                for (const s of ['#subtitle-timeline','#subtitle-list','.subtitle-line',
                                 '#transcript','[data-sentence-index]']) {
                    const el = document.querySelector(s);
                    if (el && el.innerText.trim().length > 20)
                        return {sel: s, len: el.innerText.trim().length};
                }
                return {sel: null, len: 0};
            }""")
            rec("14 六级材料原文已渲染", transcript["len"] > 20,
                json.dumps(transcript, ensure_ascii=False))

            fatal = [e for e in page_errors]
            rec("15 无致命 JS 错误", not fatal,
                json.dumps(fatal[:3], ensure_ascii=False))

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)

    passed = sum(1 for s in STEPS if s["pass"])
    total = len(STEPS)
    result = {
        "test": "T15 从交付说明启动，两份不同级别素材走完整流程",
        "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                               capture_output=True, text=True).stdout.strip(),
        "port": args.port, "passed": passed, "total": total, "steps": STEPS,
    }
    (out_dir / "t15-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"  T15：{passed}/{total} 通过")
    print(f"  HEAD：{result['head']}")
    print(f"  证据：{out_dir}/t15-result.json")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
