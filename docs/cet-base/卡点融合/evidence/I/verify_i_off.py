"""包 I · I3 验证：ELT_CET_FEATURE_CET_LISTENING_AGENT=0 时，精听页不出现筛选/标记，
且不发 /api/cet/marks 请求。

前置：真实服务（默认 http://127.0.0.1:5304）以 ELT_CET_FEATURE_CET_LISTENING_AGENT=0 启动。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:5304")
EVIDENCE = Path(__file__).resolve().parent
RESULT: dict = {"base": BASE, "steps": []}


def rec(name: str, ok: bool, actual) -> None:
    RESULT["steps"].append({"step": name, "pass": bool(ok), "actual": actual})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual}")


def check(browser, width: int, height: int, shot: str) -> None:
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "/api/cet/" in r.url else None)
    page.add_init_script("localStorage.setItem('cet.session.v1','cet-session-i-test')")
    page.goto(BASE + "/workspace/1/intensive", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(900)
    rec(f"{width} 无「我的卡点」筛选", page.locator('[data-filter="marks"]').count() == 0, page.locator('[data-filter="marks"]').count())
    rec(f"{width} 无卡点标记", page.locator(".sentence-cet-mark").count() == 0, page.locator(".sentence-cet-mark").count())
    rec(f"{width} 不发 /api/cet/* 请求", requests == [], requests)
    rec(f"{width} 精听正文正常", page.locator(".sentence-card").count() == 32, page.locator(".sentence-card").count())
    page.screenshot(path=str(EVIDENCE / shot), full_page=True)
    ctx.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        check(browser, 1440, 900, "06-module-off-1440.png")
        check(browser, 375, 812, "07-module-off-375.png")
        browser.close()
    passed = sum(1 for step in RESULT["steps"] if step["pass"])
    RESULT["summary"] = {"passed": passed, "total": len(RESULT["steps"]), "allPass": passed == len(RESULT["steps"])}
    (EVIDENCE / "verify_i_off_result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(RESULT["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
