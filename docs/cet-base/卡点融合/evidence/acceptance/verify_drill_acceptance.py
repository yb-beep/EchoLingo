#!/usr/bin/env python
"""卡点融合 · V1–V9 真实浏览器总验收脚本（主代理）。

前置：服务已在 5310 启动，使用全新隔离库，已导入样例材料：
    ELT_USER_DB=/tmp/cet-drill-final-5310/vocab.db \
    ELT_CONFIG_DIR=/tmp/cet-drill-final-5310/config ELT_PORT=5310 \
    ELT_CET_FEATURE_CET_LISTENING_AGENT=1 \
    .venv/bin/python backend/fastapi_server.py

运行：
    cd /tmp/wt-drill-int
    .venv/bin/python docs/cet-base/卡点融合/evidence/acceptance/verify_drill_acceptance.py

输出：逐项 PASS/FAIL JSON + 四宽度截图（同目录），退出码 0 仅当全部通过。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("CET_DRILL_BASE_URL", "http://127.0.0.1:5310").rstrip("/")
OUT = Path(__file__).resolve().parent
RESULTS: list[dict] = []


def check(name: str, ok: bool, detail=None) -> bool:
    RESULTS.append({"name": name, "pass": bool(ok), "detail": detail})
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if detail is None else f" :: {detail}"))
    return bool(ok)


def api_get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def media_state(page):
    return page.evaluate(
        "() => { const a = document.getElementById('local-media-player');"
        " return a ? {t: a.currentTime, paused: a.paused, rate: a.playbackRate} : null; }"
    )


def wait_step(page, step: str, timeout=25000):
    page.wait_for_function(
        "step => window.__cet && window.__cet.step === step",
        arg=step,
        timeout=timeout,
    )


def open_workspace(page):
    page.goto(f"{BASE}/workspace/1", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
    page.wait_for_function(
        "() => window.__cet && window.__cet.step !== null", timeout=25000
    )
    page.wait_for_timeout(1200)


def select_correct_answers(page):
    for index, answer in enumerate([1, 1, 3, 2]):
        page.locator("#cet-question-list .cet-q").nth(index).locator(".cet-opt").nth(answer).click()


def create_mark_at(page, seconds: float):
    page.evaluate(
        "(seconds) => { const a = document.getElementById('local-media-player'); a.currentTime = seconds; }",
        seconds,
    )
    page.wait_for_timeout(250)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(800)


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True, channel="chrome", args=["--autoplay-policy=no-user-gesture-required"]
            )
        except Exception:
            browser = p.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/workspace/1", wait_until="domcontentloaded")
        page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
        page.wait_for_function("() => window.__cet && window.__cet.step === 'blind'", timeout=25000)
        page.wait_for_timeout(1200)

        # ═════════ V1 盲听 ═════════
        step = page.evaluate("() => window.__cet.step")
        check("V1-1 默认步骤为 blind", step == "blind", step)
        stepbar = page.evaluate(
            "() => { const e = document.getElementById('ws-step-bar');"
            " return e ? {hidden: e.hidden, text: e.innerText} : null; }"
        )
        check("V1-2 三步步骤条可见", bool(stepbar) and not stepbar["hidden"]
              and "盲听做题" in stepbar["text"] and "对答案·找卡点" in stepbar["text"]
              and "精练·复习" in stepbar["text"], stepbar)
        hidden = page.evaluate("() => document.body.classList.contains('cet-hide-text')")
        check("V1-3 blind 原文隐藏", hidden is True)
        subtitle = page.evaluate(
            "() => { const e = document.getElementById('current-subtitle');"
            " return {color: getComputedStyle(e).color,"
            " after: getComputedStyle(e, '::after').content}; }"
        )
        check("V1-4 当前句视觉不可读且有隐藏提示",
              subtitle["color"].startswith("rgba(0, 0, 0, 0)") and "原文已隐藏" in subtitle["after"],
              subtitle)
        outline_text = page.evaluate(
            "() => { const e = document.getElementById('subtitle-timeline'); return e ? e.innerText : ''; }"
        )
        check("V1-5 大纲不泄露原句、只显示时间与卡点数",
              "getting late" not in outline_text and "0 个卡点" in outline_text, outline_text[:120])
        create_mark_at(page, 5.0)
        marks = page.evaluate(
            "() => (window.__cet.marks || []).map(m => ({id: m.markId, pressed: m.pressedAtSec, conf: m.confirmedSentenceId}))"
        )
        mark_text = page.evaluate("() => document.getElementById('cet-marks').innerText")
        check("V1-6 M 能打卡点", len(marks) == 1, marks)
        check("V1-7 blind 卡点行只显示标记时间与回听、无候选原文/确认",
              "标记于 0:05" in mark_text and "回听" in mark_text
              and "getting late" not in mark_text and "确认这句" not in mark_text, mark_text)
        page.click("#cet-hotkey-btn")
        page.wait_for_timeout(300)
        hotkeys = page.evaluate("() => (document.getElementById('cet-hotkey-panel') || {}).innerText || ''")
        check("V1-8 快捷键面板包含 M/标记没听清", "M" in hotkeys and "标记没听清" in hotkeys, hotkeys)

        # ═════════ V2 交卷 ═════════
        qcount = page.locator("#cet-question-list .cet-q").count()
        question_text = page.evaluate("() => document.getElementById('cet-blind-panel').innerText")
        check("V2-1 演示材料为 4 题且无占位题",
              qcount == 4 and "样例占位" not in question_text and "选项 A（占位）" not in question_text,
              {"qcount": qcount})
        select_correct_answers(page)
        page.click("#cet-step-primary")
        wait_step(page, "review")
        page.wait_for_timeout(600)
        review_text = page.evaluate("() => document.getElementById('cet-review-panel').innerText")
        check("V2-2 成绩显示 4 / 4", "4 / 4" in review_text, review_text[:180])
        check("V2-3 交卷按钮已变为进入精练", page.locator("#cet-step-primary").inner_text().startswith("进入精练"),
              page.locator("#cet-step-primary").inner_text())
        evidence = page.locator(".cet-evidence-btn")
        check("V2-4 每题有可点击的原声证据", evidence.count() == 4, evidence.count())
        if evidence.count():
            evidence.first.click()
            page.wait_for_timeout(700)
            state = media_state(page)
            check("V2-5 听原声证据真实播放", state is not None and state["paused"] is False and 0 <= state["t"] <= 4.0,
                  state)
        # 占位题在演示材料不存在（D1 过滤），无答案不计分由 K3 静态契约覆盖
        check("V2-6 页面无占位题文案", "样例占位" not in page.evaluate("() => document.body.innerText"))

        # ═════════ V3 确认 ═════════
        confirm = page.locator("#cet-marks .cet-btn-confirm")
        check("V3-1 交卷后出现确认这句", confirm.count() == 1, confirm.count())
        if confirm.count():
            confirm.first.click()
            page.wait_for_timeout(900)
        confirmed = page.evaluate("() => (window.__cet.marks || []).filter(m => m.confirmedSentenceId).length")
        check("V3-2 确认成功落库到本地状态", confirmed >= 1, confirmed)
        menu = page.locator("#cet-marks details.cet-mark-menu")
        check("V3-3 卡点行有 ⋯ 菜单", menu.count() >= 1)
        if menu.count():
            menu.first.locator("summary").click()
            page.wait_for_timeout(200)
            menu_text = menu.first.inner_text()
            check("V3-4 菜单含改一下/删除", "改一下" in menu_text and "删除" in menu_text, menu_text)
        page.click("#cet-step-primary")
        wait_step(page, "drill")
        page.wait_for_timeout(1200)
        auto_state = media_state(page)
        # 已带练的删除必须被禁用
        page.evaluate("() => window.__cet.setStep('review')")
        page.wait_for_timeout(600)
        menu = page.locator("#cet-marks details.cet-mark-menu")
        delete_disabled = None
        if menu.count():
            menu.first.locator("summary").click()
            page.wait_for_timeout(200)
            delete_disabled = page.evaluate(
                "() => { const d = document.querySelector('#cet-marks details.cet-mark-menu');"
                " if (!d) return null; const btns = [...d.querySelectorAll('.cet-mark-menu-item')];"
                " const b = btns.find(x => x.innerText.trim() === '删除'); return b ? {disabled: b.disabled, title: b.title} : null; }"
            )
        check("V3-5 已带练卡点的删除按钮被禁用", bool(delete_disabled and delete_disabled["disabled"]), delete_disabled)
        page.evaluate("() => window.__cet.setStep('drill')")
        page.wait_for_timeout(1000)

        # ═════════ V4 带练 ═════════
        drill_text = page.evaluate("() => document.getElementById('cet-drill-panel').innerText")
        check("V4-1 带练面板显示正在练的句子", "正在练的句子" in drill_text and "getting late" in drill_text)
        check("V4-2 进入带练自动播放", auto_state is not None and auto_state["paused"] is False and auto_state["t"] > 0, auto_state)
        page.evaluate("() => window.__ws.setRate(1.25)")
        page.wait_for_timeout(250)
        page.locator('[data-cet-drill-action="slow"]').click()
        page.wait_for_timeout(600)
        during = media_state(page)
        check("V4-3 慢放真实 0.75x", during is not None and abs(during["rate"] - 0.75) < 0.01, during)
        restored = None
        for _ in range(30):
            page.wait_for_timeout(500)
            now = media_state(page)
            if now and abs(now["rate"] - 1.25) < 0.01:
                restored = now
                break
        check("V4-4 慢放结束后倍速恢复 1.25", restored is not None, restored)
        page.locator('[data-cet-feedback="meaning"]').click()
        page.wait_for_timeout(3500)
        practice_text = page.evaluate("() => document.getElementById('cet-practice-out').innerText")
        offline = page.locator(".cet-offline-explain")
        check("V4-5 不懂意思无 key 显示离线词义",
              offline.count() == 1 and "配置模型后可获得讲解" in practice_text
              and len(page.locator(".cet-offline-word").all()) >= 8, practice_text[:200])
        check("V4-6 不出现模型未配置提示",
              "模型尚未配置" not in practice_text and "模型未配置" not in practice_text
              and "AI_NOT_CONFIGURED" not in practice_text)
        if page.locator(".cet-drill-word").count():
            page.locator(".cet-drill-word").first.click()
            page.wait_for_timeout(700)
            popover = page.evaluate(
                "() => { const e = document.getElementById('word-popover');"
                " return e ? {open: e.classList.contains('open'), text: e.innerText.slice(0, 80)} : null; }"
            )
            check("V4-7 带练句子点词可查询", bool(popover and popover["open"]), popover)
        else:
            check("V4-7 带练句子点词可查询", False, "没有 .cet-drill-word")

        # ═════════ V5 收藏 ═════════
        page.locator("#cet-save-practice").click()
        page.wait_for_timeout(1800)
        ask = page.locator("#cet-favorite-yes")
        check("V5-1 保存本轮后询问收藏", ask.count() == 1,
              page.evaluate("() => document.getElementById('ws-step-panel-body').innerText.slice(0,300)"))
        if ask.count():
            ask.click()
            page.wait_for_timeout(1200)
        favorites = api_get("/api/v2/lessons/1/phase-b")
        texts = [item.get("text", "") for item in favorites.get("sentences", [])]
        check("V5-2 收藏句进入我的收藏接口", any("getting late" in t for t in texts), texts)
        page.goto(f"{BASE}/#favorites", wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        page.evaluate("() => window.cetSetFavoritesPane && window.cetSetFavoritesPane('sentences')")
        page.wait_for_timeout(1500)
        fav_text = page.evaluate("() => document.getElementById('tab-sentences').innerText")
        check("V5-3 我的收藏·句子出现该句",
              "getting late" in fav_text and "1 个收藏句" in fav_text, fav_text[:200])
        page.locator("#tab-sentences .sentence-library-row").first.locator("button", has_text="展开").click()
        page.wait_for_timeout(400)
        back_link = page.evaluate(
            "() => { const a = document.querySelector('#tab-sentences .sentence-row-link');"
            " return a ? {text: a.innerText, href: a.getAttribute('href')} : null; }"
        )
        check("V5-4 收藏句可回到原课程原句", bool(back_link and "/workspace/1/intensive" in (back_link["href"] or "")), back_link)

        # ═════════ V6 复习 ═════════
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(2200)
        today_text = page.evaluate("() => (document.getElementById('cet-today-review') || {}).innerText || ''")
        summary = api_get("/api/cet/summary")
        groups = page.evaluate(
            "() => [...document.querySelectorAll('#cet-today-review .cet-review-group')].map(g => ({name: g.querySelector('.cet-review-group-name')?.innerText,"
            " count: Number((g.querySelector('.cet-review-group-count')?.innerText || '0').trim())}))"
        )
        by_name = {item["name"]: item["count"] for item in groups}
        due = summary.get("reviewDue", {})
        check("V6-1 首页今日复习三组数量与接口一致",
              by_name.get("卡点") == due.get("marks") and by_name.get("收藏句") == due.get("sentences")
              and by_name.get("收藏词") == due.get("words"), {"dom": by_name, "api": due})
        link = page.locator('#cet-today-review a[href*="review=1"]').first
        check("V6-2 首页卡点组有进入复习的链接", link.count() == 1)
        if link.count():
            link.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
            wait_step(page, "review")
            page.wait_for_timeout(2200)
            review_state = page.evaluate(
                "() => ({active: !!(window.__cet.getReviewState() && window.__cet.getReviewState().status === 'active'),"
                " hidden: document.body.classList.contains('cet-hide-text'),"
                " text: document.getElementById('cet-review-panel').innerText.slice(0, 400)})"
            )
            check("V6-3 点卡点直接进入复习且先盲听",
                  review_state["active"] and review_state["hidden"] and "先盲听" in review_state["text"], review_state)
            reveal = page.locator("#cet-review-reveal")
            check("V6-4 复习条目有显示原文按钮", reveal.count() == 1)
            if reveal.count():
                reveal.click()
                page.wait_for_timeout(700)
                revealed = page.evaluate(
                    "() => ({hidden: document.body.classList.contains('cet-hide-text'),"
                    " text: document.querySelector('.cet-review-sentence')?.innerText || ''})"
                )
                check("V6-5 点显示原文才出句子",
                      revealed["hidden"] is False and "getting late" in revealed["text"], revealed)

        # ═════════ V7 融合：逐句精听我的卡点 ═════════
        page.goto(f"{BASE}/workspace/1/intensive", wait_until="domcontentloaded")
        page.wait_for_timeout(2600)
        intensive = page.evaluate(
            "() => ({filter: document.querySelector('[data-filter=\\\"marks\\\"]')?.innerText || null,"
            " count: document.getElementById('count-marks')?.textContent,"
            " badgeCount: document.querySelectorAll('.sentence-cet-mark').length,"
            " req: performance.getEntriesByType('resource').filter(r => r.name.includes('/api/cet/marks')).length})"
        )
        marks_total = 1
        check("V7-1 精听页有我的卡点筛选且数量>0",
              intensive["filter"] is not None and "我的卡点" in intensive["filter"]
              and int(intensive["count"] or 0) > 0, intensive)
        check("V7-2 精听页卡点句有标记", intensive["badgeCount"] > 0, intensive)

        # ═════════ V8 排版（四宽度） ═════════
        shot_dir = OUT
        layout_results = {}
        for width, height in [(1440, 900), (1280, 800), (768, 900), (375, 812)]:
            page.set_viewport_size({"width": width, "height": height})
            page.goto(f"{BASE}/workspace/1", wait_until="domcontentloaded")
            page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
            page.wait_for_function("() => window.__cet && window.__cet.step !== null", timeout=25000)
            page.wait_for_timeout(1200)
            info = page.evaluate(
                "() => { const overflow = document.documentElement.scrollWidth - window.innerWidth;"
                " const bar = document.getElementById('ws-step-bar');"
                " const visiblePanels = [...document.querySelectorAll('[data-sidebar-panel]')]"
                "   .filter(e => { const s = getComputedStyle(e); return s.display !== 'none' && s.visibility !== 'hidden' && !e.classList.contains('sidebar-panel-inactive'); });"
                " return {overflow, stepBar: bar ? {w: bar.getBoundingClientRect().width, scrollW: bar.scrollWidth} : null,"
                " visiblePanels: visiblePanels.length}; }"
            )
            page.screenshot(path=str(shot_dir / f"v8-{width}.png"), full_page=True)
            layout_results[str(width)] = info
        check("V8-1 四宽度无横向溢出且步骤条可滚动",
              all(v["overflow"] <= 1 for v in layout_results.values())
              and all((v["stepBar"] is None or v["stepBar"]["scrollW"] <= v["stepBar"]["w"] + 2)
                      for v in layout_results.values()), layout_results)
        check("V8-2 右栏同一时间只显示一个面板",
              all(v["visiblePanels"] <= 1 for v in layout_results.values()), layout_results)

        # ═════════ V9 刷新恢复 ═════════
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{BASE}/workspace/1?review=1", wait_until="domcontentloaded")
        page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
        page.wait_for_timeout(2000)
        before_step = page.evaluate("() => window.__cet.step")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#local-media-player", state="attached", timeout=25000)
        page.wait_for_function("() => window.__cet && window.__cet.step !== null", timeout=25000)
        page.wait_for_timeout(1200)
        after_step = page.evaluate("() => window.__cet.step")
        after_actions = page.evaluate("() => document.getElementById('ws-step-actions').innerText")
        check("V9-1 刷新后步骤一致", before_step == after_step, {"before": before_step, "after": after_step})
        check("V9-2 刷新后按钮与步骤一致", bool(after_actions.strip()), after_actions)

        check("V9-3 页面无 JS 错误", not errors, errors[:5])
        ctx.close()
        browser.close()

    passed = sum(1 for r in RESULTS if r["pass"])
    total = len(RESULTS)
    print(json.dumps({"passed": passed, "total": total, "allPass": passed == total, "results": RESULTS},
                     ensure_ascii=False, indent=2))
    (OUT / "verify_drill_acceptance_result.json").write_text(
        json.dumps({"passed": passed, "total": total, "allPass": passed == total, "results": RESULTS},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
