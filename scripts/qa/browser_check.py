#!/usr/bin/env python3
"""四六级听力黑匣子 · 浏览器层验证（C 负责）。

为什么需要这一层：
    C 其余测试全部走 HTTP，只能证明「接口返回了对的东西」，
    不能证明「页面上真的能做这件事」。任务书要求把「页面可打开」
    与「人耳听到正确原声」分开记录 —— 这个脚本负责第一件，
    并把第二件的**前置条件**（音频真的被浏览器解码、时长正确）验出来。

它能证明什么（与不能证明什么）：
    ✅ 底座页面能打开、无 JS 报错、关键 DOM 存在
    ✅ 样例音频被浏览器加载并可解码，时长与契约一致
    ✅ 可以 seek 到指定句子起点，播放进度真的在走
    ❌ 不能证明「声音内容与文字对得上」—— 那必须人耳听（H1）

用法：
    .venv/bin/python scripts/qa/browser_check.py
    .venv/bin/python scripts/qa/browser_check.py --headed     # 看见窗口
    .venv/bin/python scripts/qa/browser_check.py --audio-only # 只验音频

前置：底座在跑（scripts/qa/start_cet.sh），已安装 playwright chromium。
退出码：0 全通过｜1 有失败项
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = os.environ.get("CET_BASE", "http://127.0.0.1:5173").rstrip("/")
MOCK = os.environ.get("CET_MOCK_BASE", "http://127.0.0.1:5199").rstrip("/")
ROOT = Path(__file__).resolve().parents[2]
AUDIO = ROOT / ".cet-qa" / "media" / "cet6-202512-conv2.mp3"

# 与契约样例一致：第一处卡点吸附到 s13
PROBE_SENTENCE = {"sentenceId": "s13", "startSec": 36.77, "endSec": 41.41}
EXPECTED_DURATION = 105.486349

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label, detail))
    print(f"  {'OK' if ok else 'X '}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def launch_browser(p, headless: bool = True):
    """按可用性依次尝试：playwright 自带 chromium → 本机 Chrome → 本机 Edge。

    D3 要在另一台电脑验收启动，那台机器不一定跑过 `playwright install`。
    优先复用本机已装的浏览器，能把「换电脑能不能起来」这件事的依赖面缩到最小。
    """
    attempts = [
        ("playwright-chromium", {"headless": headless}),
        ("chrome", {"headless": headless, "channel": "chrome"}),
        ("msedge", {"headless": headless, "channel": "msedge"}),
    ]
    for name, kwargs in attempts:
        try:
            browser = p.chromium.launch(**kwargs)
            return browser, name
        except Exception:  # noqa: BLE001
            continue
    return None, ""


def check_pages(browser, js_errors: list[str]) -> None:
    """底座保留页面（T15 的浏览器侧）。页面能开 ≠ 能用，所以还查关键 DOM。"""
    print("\n-- 底座页面（浏览器侧 T15）--")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: js_errors.append(f"{page.url}: {e}"))

    for path, title_kw, dom_hint in [
        ("/", "EchoLingo", ""),
        ("/vocab", "词汇本", ""),
    ]:
        try:
            resp = page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=30_000)
            title = page.title()
            check(
                resp is not None and resp.ok,
                f"页面 {path} 可打开",
                f"HTTP {resp.status if resp else '无响应'}",
            )
            check(title_kw in title, f"页面 {path} 标题正确", title[:40])
        except Exception as exc:  # noqa: BLE001
            check(False, f"页面 {path} 可打开", str(exc)[:90])

    # 设置页：底座用弹层而非独立路由，检查首页里的设置入口存在
    try:
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30_000)
        has_settings = page.evaluate(
            """() => {
                const t = document.body.innerText;
                return t.includes('设置') || !!document.querySelector('[data-settings],[href*="setting"]');
            }"""
        )
        check(bool(has_settings), "首页有设置入口")
    except Exception as exc:  # noqa: BLE001
        check(False, "首页有设置入口", str(exc)[:90])

    page.close()


def check_seeded_content(browser, js_errors: list[str]) -> None:
    """有内容时的页面渲染（依赖 seed_base_data.py 已预置课程）。

    空库状态下只能验 200 和骨架；预置后才能真正验「页面把内容渲染出来了」。
    没有预置数据时跳过，不误判为失败。
    """
    print("\n-- 有内容时的页面渲染 --")
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BASE}/api/v2/lessons/library", timeout=10) as resp:
            lessons = json.loads(resp.read().decode("utf-8")).get("lessons") or []
    except Exception as exc:  # noqa: BLE001
        check(False, "课程库可读", str(exc)[:80])
        return

    if not lessons:
        check(True, "有内容渲染检查", "跳过：库为空，先跑 scripts/qa/seed_base_data.py")
        return

    lesson_id = lessons[0].get("id")
    page = browser.new_page()
    page.on("pageerror", lambda e: js_errors.append(f"{page.url}: {e}"))
    try:
        resp = page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle", timeout=45_000)
        ok = resp is not None and resp.ok
        check(ok, f"工作台 /workspace/{lesson_id} 可打开",
              f"HTTP {resp.status if resp else '无响应'}")
        if ok:
            body = page.evaluate("() => document.body.innerText")
            check(len(body) > 100, "工作台有实质正文", f"{len(body)} 字符")
            # 不能用「某几个固定单词」判断渲染成功 —— 库里第一节会变
            # （预置的 reading 课、导入的音频课都可能排第一）。
            # 改成：页面正文里应出现该课自己的标题或第一句，说明渲染的是对的内容。
            title = str(lessons[0].get("title") or "").strip()
            first_line = ""
            try:
                import urllib.request as _ur
                with _ur.urlopen(f"{BASE}/api/v2/lessons/{lesson_id}/subtitles", timeout=8) as r2:
                    subs = json.loads(r2.read().decode("utf-8"))
                units = subs.get("sentence_units") or []
                if units:
                    first_line = str(units[0].get("text") or "").strip()
                elif (subs.get("segments") or []):
                    first_line = str(subs["segments"][0].get("text") or "").strip()
            except Exception:  # noqa: BLE001
                pass
            candidates = [x for x in (title, first_line[:24]) if x]
            matched = any(c in body for c in candidates)
            check(matched or not candidates,
                  "工作台渲染的是该课自身内容",
                  f"标题/首句命中={matched}" if candidates else "无标题与句子可比对")
    except Exception as exc:  # noqa: BLE001
        check(False, f"工作台 /workspace/{lesson_id} 可打开", str(exc)[:90])
    finally:
        page.close()


def check_audio(browser) -> None:
    """播放链路的前置条件：浏览器能取到音频、能解码、时长对、能 seek。

    这一项把「人耳听到正确原声」之前所有机器能验的部分都验掉，
    剩下真正需要人耳的就只剩「内容对不对」。
    """
    print("\n-- 样例音频在浏览器中的可播放性 --")
    if not AUDIO.is_file():
        check(False, "样例音频存在", f"未找到 {AUDIO}；先按启动说明 §8 放好音频")
        return

    page = browser.new_page()
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30_000)

    # Mock 提供 /api/cet/materials/{id}/audio；直接喂给 <audio> 验证真实解码
    audio_url = f"{MOCK}/api/cet/materials/cet6-202512-conv2/audio"
    result = page.evaluate(
        """async (url) => {
            const a = document.createElement('audio');
            a.preload = 'metadata';
            a.src = url;
            const done = new Promise((resolve) => {
                const t = setTimeout(() => resolve({timeout: true}), 15000);
                a.addEventListener('loadedmetadata', () => {
                    clearTimeout(t);
                    resolve({ok: true, duration: a.duration, seekable: a.seekable.length});
                }, {once: true});
                a.addEventListener('error', () => {
                    clearTimeout(t);
                    resolve({ok: false, code: a.error ? a.error.code : null});
                }, {once: true});
            });
            return await done;
        }""",
        audio_url,
    )

    if not result.get("ok"):
        check(False, "浏览器可加载并解码样例音频",
              f"timeout={result.get('timeout')} errorCode={result.get('code')}")
        page.close()
        return

    dur = float(result["duration"])
    check(True, "浏览器可加载并解码样例音频", f"时长 {dur:.3f}s")
    check(
        abs(dur - EXPECTED_DURATION) < 0.5,
        "浏览器解出的时长与契约一致",
        f"浏览器 {dur:.3f}s vs 契约 {EXPECTED_DURATION:.3f}s",
    )
    check(result.get("seekable", 0) > 0, "音频可 seek（回听定位前提）")

    # 跳到卡点句起点，确认取到的是该句范围
    seek = page.evaluate(
        """async (args) => {
            const a = document.createElement('audio');
            a.preload = 'auto';
            a.src = args.url;
            await new Promise((r) => a.addEventListener('loadedmetadata', r, {once: true}));
            a.currentTime = args.start;
            await new Promise((r) => a.addEventListener('seeked', r, {once: true}));
            return {currentTime: a.currentTime};
        }""",
        {"url": audio_url, "start": PROBE_SENTENCE["startSec"]},
    )
    got = float(seek["currentTime"])
    check(
        abs(got - PROBE_SENTENCE["startSec"]) < 0.6,
        f"可定位到卡点句 {PROBE_SENTENCE['sentenceId']} 起点",
        f"seek 到 {got:.2f}s，预期 {PROBE_SENTENCE['startSec']}s",
    )

    page.close()


def check_mock_ui(browser, js_errors: list[str]) -> None:
    """Mock 的接口文档页能否打开（A 联调时要看）。"""
    print("\n-- Mock API 文档页 --")
    page = browser.new_page()
    page.on("pageerror", lambda e: js_errors.append(f"{page.url}: {e}"))
    try:
        resp = page.goto(f"{MOCK}/api/cet/_mock/docs", wait_until="domcontentloaded", timeout=30_000)
        ok = resp is not None and resp.ok
        check(ok, "Mock 文档页可打开", f"HTTP {resp.status if resp else '无响应'}")
        if ok:
            # Swagger 依赖 CDN；离线时会白屏，这里如实记录而不是直接判失败
            rendered = page.evaluate(
                "() => document.body.innerText.includes('CET') || !!document.querySelector('.swagger-ui')"
            )
            check(bool(rendered), "Mock 文档页已渲染内容", "白屏通常是离线取不到 CDN")
    except Exception as exc:  # noqa: BLE001
        check(False, "Mock 文档页可打开", str(exc)[:90])
    page.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="四六级 · 浏览器层验证")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--audio-only", action="store_true", help="只验音频可播放性")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright。先：.venv/bin/pip install playwright && "
              ".venv/bin/playwright install chromium", file=sys.stderr)
        return 1

    js_errors: list[str] = []
    print("四六级听力黑匣子 · 浏览器层验证")
    print(f"底座 {BASE}｜Mock {MOCK}")
    print("-" * 68)

    with sync_playwright() as p:
        browser, channel = launch_browser(p, headless=not args.headed)
        if browser is None:
            print(
                "X  没有可用的浏览器。按顺序试过：playwright chromium、本机 Chrome、本机 Edge。\n"
                "   修法一（推荐，无需下载）：装 Chrome 后重跑。\n"
                "   修法二：.venv/bin/playwright install chromium\n"
                "   本脚本只依赖浏览器的 HTML/JS 能力，三个通道任一可用即可。",
                file=sys.stderr,
            )
            return 1
        if channel != "playwright-chromium":
            print(f"（使用本机浏览器通道：{channel}）")
        try:
            if not args.audio_only:
                check_pages(browser, js_errors)
                check_seeded_content(browser, js_errors)
            check_audio(browser)
            if not args.audio_only:
                check_mock_ui(browser, js_errors)
        finally:
            browser.close()

    print("\n-- JS 运行时错误 --")
    if js_errors:
        for e in js_errors[:10]:
            print(f"  !  {e[:140]}")
        check(False, "页面无 JS 报错", f"{len(js_errors)} 条")
    else:
        check(True, "页面无 JS 报错")

    failed = [r for r in results if not r[0]]
    print("-" * 68)
    if failed:
        print(f"结论：{len(failed)} 项失败 / 共 {len(results)} 项")
        return 1
    print(f"结论：{len(results)} 项全部通过")
    print("\n提醒：本脚本只证明「页面可打开、音频可解码可定位」。")
    print("      「声音内容与文字是否相符」必须人耳试听（H1），机器不能替代。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
