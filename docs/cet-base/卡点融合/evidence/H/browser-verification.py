import json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5303"
OUT = Path("/tmp/wt-drill-h/docs/cet-base/卡点融合/evidence/H")
results = []

def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("PASS" if ok else "FAIL"), name, "|", detail)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#course-library-list .cet-material-card", timeout=15000)
    page.wait_for_timeout(600)

    cards = page.locator("#course-library-list .cet-material-card")
    check("材料卡数量 >= 2", cards.count() >= 2, f"count={cards.count()}")
    progress_texts = page.locator(".cet-material-progress").all_text_contents()
    check("材料卡摘要可见", any("已交卷" in t for t in progress_texts), json.dumps(progress_texts, ensure_ascii=False))
    check("材料卡摘要含卡点", any("卡点" in t for t in progress_texts), json.dumps(progress_texts, ensure_ascii=False))
    check("材料卡摘要含待复习", any("待复习" in t for t in progress_texts), json.dumps(progress_texts, ensure_ascii=False))

    groups = page.locator("#cet-today-review-groups .cet-review-group")
    check("今日复习三组", groups.count() == 3, f"count={groups.count()}")
    counts = page.locator("#cet-today-review-groups .cet-review-group-count").all_text_contents()
    check("今日复习数量 1/1/1", counts == ["1", "1", "1"], json.dumps(counts))
    group_names = page.locator("#cet-today-review-groups .cet-review-group-name").all_text_contents()
    check("今日复习组名", group_names == ["卡点", "收藏句", "收藏词"], json.dumps(group_names, ensure_ascii=False))
    check("今日复习卡在 CET 首页", page.locator("#cet-today-review").is_visible(), "")

    page.screenshot(path=str(OUT / "home-1440.png"), full_page=True)

    # 卡点链接 → /workspace/<lessonId>?review=1
    mark_link = page.locator("#cet-today-review-groups .cet-review-links a").first
    href = mark_link.get_attribute("href")
    check("卡点链接带 review=1", bool(re.fullmatch(r"/workspace/\d+\?review=1", href or "")), href or "")
    mark_link.click()
    page.wait_for_load_state("networkidle")
    check("卡点点击进入对应材料", bool(re.search(r"/workspace/\d+\?review=1$", page.url)), page.url)

    # 真实媒体状态：Space 是 CET 播放/暂停快捷键（cet-player.js 接管）
    try:
        page.wait_for_selector("#local-media-player", timeout=15000)
        page.wait_for_function("() => { const a=document.querySelector('#local-media-player'); return a && a.readyState >= 1; }", timeout=15000)
        audio = page.locator("#local-media-player")
        # 真实点击原生播放器左侧播放按钮（Chromium 自绘 controls）
        audio.click(position={"x": 20, "y": 27})
        page.wait_for_timeout(900)
        state = page.evaluate("() => { const a=document.querySelector('#local-media-player'); return a ? {paused:a.paused,currentTime:a.currentTime,playbackRate:a.playbackRate,readyState:a.readyState,src:a.currentSrc} : null; }")
        check("真实媒体开始播放（paused=false 且 currentTime>0）", bool(state and not state["paused"] and state["currentTime"] > 0), json.dumps(state, ensure_ascii=False))
        audio.click(position={"x": 20, "y": 27})
        page.wait_for_timeout(400)
        state2 = page.evaluate("() => { const a=document.querySelector('#local-media-player'); return a ? {paused:a.paused,currentTime:a.currentTime,playbackRate:a.playbackRate} : null; }")
        check("真实媒体暂停（paused=true）", bool(state2 and state2["paused"]), json.dumps(state2, ensure_ascii=False))
        page.screenshot(path=str(OUT / "workspace-review-1440.png"), full_page=False)
    except Exception as exc:  # noqa: BLE001
        check("真实媒体播放验证", False, f"exception: {exc}")

    # 收藏句链接
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#cet-today-review-groups .cet-review-group", timeout=15000)
    page.get_by_role("link", name="去我的收藏 · 句子 →").click()
    page.wait_for_timeout(500)
    check("收藏句链接进入句子收藏", page.locator("#tab-sentences").evaluate("el => el.classList.contains('active')"), "")
    check("收藏句链接写 localStorage", page.evaluate("() => localStorage.getItem('cet-favorites-pane')") == "sentences", "")
    check("收藏句链接 hash=#favorites", page.evaluate("() => location.hash") == "#favorites", page.evaluate("() => location.hash"))

    # 收藏词链接
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#cet-today-review-groups .cet-review-group", timeout=15000)
    page.get_by_role("link", name="去我的收藏 · 词汇 →").click()
    page.wait_for_timeout(500)
    check("收藏词链接进入词汇收藏", page.locator("#tab-vocab").evaluate("el => el.classList.contains('active')"), "")
    check("收藏词链接写 localStorage", page.evaluate("() => localStorage.getItem('cet-favorites-pane')") == "vocab", "")

    # 375 宽度
    mobile = browser.new_context(viewport={"width": 375, "height": 812})
    mpage = mobile.new_page()
    mpage.goto(BASE + "/", wait_until="networkidle")
    mpage.wait_for_selector("#course-library-list .cet-material-card", timeout=15000)
    mpage.wait_for_timeout(600)
    mpage.screenshot(path=str(OUT / "home-375.png"), full_page=True)
    overflow = mpage.evaluate("() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})")
    check("375 无横向溢出", overflow["scrollWidth"] <= overflow["innerWidth"] + 1, json.dumps(overflow))
    groups_m = mpage.locator("#cet-today-review-groups .cet-review-group")
    check("375 今日复习三组单列", groups_m.count() == 3, f"count={groups_m.count()}")
    mpage.screenshot(path=str(OUT / "home-375-today-review.png"), full_page=False)

    browser.close()

print("\nRESULT_JSON=" + json.dumps(results, ensure_ascii=False))
failed = [r for r in results if not r["ok"]]
print("FAILED:", len(failed))
sys.exit(1 if failed else 0)
