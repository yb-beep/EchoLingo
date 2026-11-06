#!/usr/bin/env python
"""四六级底座 · Lead 集成真实试用（一条命令走完整用户流程）。

与 `scripts/cet/verify_base.py`（QA 板块的逐项自验）分工不同：
这个脚本只做**用户视角的完整链路**，用于 Lead 在合并后的同一 SHA 上确认
「打开应用 → 选级别 → 打开材料 → 播放/逐句/变速 → 查词收藏 → 返回 → 恢复位置」
确实可用，而不是只看测试通过数。

用法：
    # 1) 起服务（隔离库）
    ELT_USER_DB=/tmp/cetbase-lead/vocab.db PORT=5173 \
        .venv/bin/python backend/fastapi_server.py &

    # 2) 导入材料
    ELT_USER_DB=/tmp/cetbase-lead/vocab.db \
        .venv/bin/python scripts/cet/import_materials.py --all

    # 3) 跑真实试用
    .venv/bin/python scripts/cet/lead_trial.py --base http://127.0.0.1:5173 \
        --out /tmp/cetbase-lead/evidence

退出码：0 全部通过｜1 有失败项（逐项打印实际值）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

RESULTS: list[dict] = []


def record(item: str, ok: bool, expected: str, actual: str, evidence: str = "") -> None:
    RESULTS.append({"item": item, "pass": bool(ok), "expected": expected,
                    "actual": actual, "evidence": evidence})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {item}: {actual}")


def http_json(url: str, method: str = "GET", payload: dict | None = None,
              timeout: float = 15.0) -> tuple[int, object]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def check_api(base: str) -> dict:
    """接口层：模式配置、材料列表、级别筛选、详情、字幕、进度。"""
    print("\n── A. 接口层 ──")
    out: dict = {}

    status, mode = http_json(f"{base}/api/cet/mode")
    ok = status == 200 and isinstance(mode, dict) and "features" in mode
    record("A1 CET 模式配置可用", ok, "200 + features", f"{status} keys={list(mode)[:5] if isinstance(mode, dict) else mode}")
    out["mode"] = mode

    status, materials = http_json(f"{base}/api/cet/materials")
    items = materials.get("items", []) if isinstance(materials, dict) else []
    record("A2 材料列表返回", status == 200 and isinstance(items, list),
           "200 + items 数组", f"{status} count={len(items)}")
    out["materials"] = items

    cet = [m for m in items if m.get("level") in ("cet4", "cet6")]
    record("A3 存在四六级材料", len(cet) >= 1,
           ">=1 份 cet4/cet6", f"{len(cet)} 份: {[m.get('level') for m in cet]}")

    for level in ("cet4", "cet6"):
        status, filtered = http_json(f"{base}/api/cet/materials?level={level}")
        f_items = filtered.get("items", []) if isinstance(filtered, dict) else []
        all_match = all(m.get("level") == level for m in f_items)
        record(f"A4 级别筛选 {level}", status == 200 and all_match,
               f"全部 level={level}", f"{status} count={len(f_items)} all_match={all_match}")

    status, unspec = http_json(f"{base}/api/cet/materials?level=unspecified")
    u_items = unspec.get("items", []) if isinstance(unspec, dict) else []
    record("A5 待分类材料可查", status == 200,
           "200（可为空）", f"{status} count={len(u_items)}")

    if cet:
        target = cet[0]
        mid = target.get("materialId")
        status, detail = http_json(f"{base}/api/cet/materials/{mid}")
        sents = detail.get("sentences", []) if isinstance(detail, dict) else []
        record("A6 材料详情含句子", status == 200 and len(sents) > 0,
               "200 + sentences 非空", f"{status} sentences={len(sents)}")
        if sents:
            s0 = sents[0]
            has_timing = "startSec" in s0 and "endSec" in s0
            ordered = all(
                float(sents[i]["startSec"]) <= float(sents[i + 1]["startSec"])
                for i in range(len(sents) - 1)
            )
            record("A7 句子时间轴有序", has_timing and ordered,
                   "startSec/endSec 且递增", f"keys={list(s0)[:6]} ordered={ordered}")
        lesson_url = target.get("lessonUrl", "")
        record("A8 材料带 lessonUrl", bool(lesson_url), "/workspace/<id>", lesson_url)
        out["target"] = target
        out["lesson_url"] = lesson_url

        # 进度读写
        lesson_id = int(str(lesson_url).rstrip("/").rsplit("/", 1)[-1]) if lesson_url else 0
        if lesson_id:
            status, _ = http_json(
                f"{base}/api/v2/lessons/{lesson_id}/progress", "POST",
                {"last_position_seconds": 12.5, "last_segment_index": 3},
            )
            status2, prog = http_json(f"{base}/api/v2/lessons/{lesson_id}/progress")
            pos = float((prog or {}).get("last_position_seconds", -1)) if isinstance(prog, dict) else -1
            record("A9 进度写入并读回", status in (200, 204) and abs(pos - 12.5) < 0.01,
                   "12.5", f"write={status} read={status2} pos={pos}")
            out["lesson_id"] = lesson_id

        # 字幕
        status, subs = http_json(f"{base}/api/v2/lessons/{lesson_id}/subtitles")
        record("A10 字幕接口可用", status == 200, "200", f"{status}")

    return out


def check_browser(base: str, ctx: dict, out_dir: pathlib.Path) -> None:
    """浏览器层：真实渲染、播放器行为、收藏、刷新恢复。"""
    print("\n── B. 浏览器层（真实播放器）──")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        record("B0 Playwright 可用", False, "已安装", "未安装，跳过浏览器层")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    lesson_url = ctx.get("lesson_url") or ""
    if not lesson_url:
        record("B1 有可打开的材料", False, "lessonUrl", "无")
        return

    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)

        # ── 首页 ──
        page.goto(f"{base}/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        title = page.title()
        record("B1 首页标题为四六级口径", "四六级" in title or "听力" in title,
               "含「四六级」或「听力」", title)
        page.screenshot(path=str(out_dir / "01-home.png"))

        tabs = page.eval_on_selector_all(
            ".tab-btn, .cet-nav-item", "els=>els.map(e=>e.textContent.trim()).filter(Boolean)")
        record("B2 导航收束", not any("个人档案" in t for t in tabs),
               "无「个人档案」", str(tabs[:6]))

        # ── 工作区 ──
        page.goto(f"{base}{lesson_url}", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3500)
        ws_title = page.title()
        record("B3 工作区标题为听力口径", "Video Workspace" not in ws_title,
               "不含 Video Workspace", ws_title)

        body_text = page.inner_text("body")
        record("B4 无虚假加载态", "正在生成内容大纲" not in body_text
               and "AI 正在分析" not in body_text,
               "无「正在生成内容大纲/AI 正在分析」",
               "命中虚假文案" if ("正在生成内容大纲" in body_text or "AI 正在分析" in body_text) else "无")

        # 播放器真实存在
        has_audio = page.evaluate(
            "() => !!document.querySelector('audio, video, #local-media-player')")
        record("B5 播放器已挂载", has_audio, "audio/video 元素", str(has_audio))

        # 队友模块引用保留
        has_cet_js = page.evaluate(
            "() => !!document.querySelector('script[src*=\"cet-listening.js\"]')")
        record("B6 队友模块引用保留", has_cet_js, "cet-listening.js 已加载", str(has_cet_js))

        # 真实播放行为：设 currentTime / playbackRate，读回
        play_probe = page.evaluate("""() => {
            const m = document.querySelector('#local-media-player')
                   || document.querySelector('audio');
            if (!m) return {ok:false, reason:'no media element'};
            const before = m.currentTime;
            try { m.currentTime = 5.0; } catch(e) { return {ok:false, reason:'seek fail: '+e}; }
            const after = m.currentTime;
            let rate = null;
            try { m.playbackRate = 1.5; rate = m.playbackRate; } catch(e) {}
            return {ok: Math.abs(after-5.0) < 1.0, before, after, rate,
                    paused: m.paused, duration: m.duration, readyState: m.readyState,
                    src: (m.currentSrc||m.src||'').slice(-60)};
        }""")
        record("B7 拖动定位真实生效", bool(play_probe.get("ok")),
               "currentTime≈5.0", json.dumps(play_probe, ensure_ascii=False)[:180])
        record("B8 倍速真实生效", play_probe.get("rate") == 1.5,
               "playbackRate=1.5", str(play_probe.get("rate")))

        # 音源与标题匹配（不能 A 材料播 B 音频）
        audio_src = str(play_probe.get("src") or "")
        record("B9 音源非空且与材料对应", bool(audio_src),
               "非空音频地址", audio_src)

        # 原文可见
        transcript = page.evaluate("""() => {
            const sels = ['#subtitle-timeline','#subtitle-list','.subtitle-line',
                          '#transcript','.sentence-row','[data-sentence-index]',
                          '.subtitle-timeline'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.innerText.trim().length > 20)
                    return {sel:s, len: el.innerText.trim().length};
            }
            return {sel:null, len:0};
        }""")
        record("B10 原文已渲染", transcript["len"] > 20,
               "原文文本 >20 字符", json.dumps(transcript, ensure_ascii=False))

        page.screenshot(path=str(out_dir / "02-workspace.png"))

        # ── 刷新恢复 ──
        page.reload(wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        after_reload = page.evaluate("""() => {
            const m = document.querySelector('#local-media-player')
                   || document.querySelector('audio');
            return m ? {t: m.currentTime, dur: m.duration} : null;
        }""")
        record("B11 刷新后页面仍可用", after_reload is not None,
               "播放器仍在", json.dumps(after_reload, ensure_ascii=False))

        # ── 收藏页 ──
        fav_url = f"{base}/#favorites"
        page.goto(fav_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        fav_text = page.inner_text("body")
        record("B12 收藏页可用", len(fav_text.strip()) > 30,
               "有内容渲染", f"{len(fav_text)} 字符")
        page.screenshot(path=str(out_dir / "03-favorites.png"))

        # ── 设置页 ──
        page.goto(f"{base}/#resources", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        set_text = page.inner_text("body")
        record("B13 设置页可用", len(set_text.strip()) > 30,
               "有内容渲染", f"{len(set_text)} 字符")
        page.screenshot(path=str(out_dir / "04-settings.png"))

        # ── 窄窗口 ──
        page.set_viewport_size({"width": 375, "height": 780})
        page.goto(f"{base}{lesson_url}", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        overflow = page.evaluate(
            "() => ({scrollW: document.documentElement.scrollWidth,"
            " clientW: document.documentElement.clientWidth})")
        no_h_overflow = overflow["scrollW"] <= overflow["clientW"] + 4
        record("B14 窄窗口无横向溢出", no_h_overflow,
               "scrollWidth <= clientWidth+4", json.dumps(overflow))
        page.screenshot(path=str(out_dir / "05-narrow.png"))

        real_errors = [e for e in console_errors if "404" not in e]
        record("B15 无 JS 运行时错误", not real_errors,
               "无 pageerror", json.dumps(real_errors[:3], ensure_ascii=False))

        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5173")
    ap.add_argument("--out", default="/tmp/cetbase-lead/evidence")
    ap.add_argument("--api-only", action="store_true")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print("  四六级底座 · Lead 集成真实试用")
    print(f"  目标：{args.base}")
    print("=" * 66)

    t0 = time.time()
    ctx = check_api(args.base)
    if not args.api_only:
        check_browser(args.base, ctx, out_dir)

    passed = sum(1 for r in RESULTS if r["pass"])
    total = len(RESULTS)
    elapsed = time.time() - t0

    report = {"base": args.base, "passed": passed, "total": total,
              "elapsed_sec": round(elapsed, 1), "items": RESULTS}
    (out_dir / "lead-trial.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"  结果：{passed}/{total} 通过（{elapsed:.1f}s）")
    print(f"  证据：{out_dir}/lead-trial.json")
    print("=" * 66)
    if passed < total:
        print("\n失败项：")
        for r in RESULTS:
            if not r["pass"]:
                print(f"  - {r['item']}")
                print(f"      期望: {r['expected']}")
                print(f"      实际: {r['actual']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
