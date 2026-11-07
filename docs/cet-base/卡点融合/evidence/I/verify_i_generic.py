"""包 I 回归：ELT_CET_MODE=0（通用模式）下，精听页行为与基线 c4a480d 一致。

做法：同一浏览器、同一视口、同一 DB，分别截取本分支与基线工作树的精听页全页截图，
逐像素比较；同时确认不出现卡点 UI、不发 /api/cet/* 请求。

前置：
  - 本分支服务 BASE（默认 5304）以 ELT_CET_MODE=0 启动
  - 基线 c4a480d 服务 BASE_BASELINE（默认 5319）以 ELT_CET_MODE=0 启动
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:5304")
BASE_BASELINE = os.environ.get("BASE_BASELINE", "http://127.0.0.1:5319")
EVIDENCE = Path(__file__).resolve().parent
RESULT: dict = {"base": BASE, "baseline": BASE_BASELINE, "steps": []}


def rec(name: str, ok: bool, actual) -> None:
    RESULT["steps"].append({"step": name, "pass": bool(ok), "actual": actual})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual}")


def snapshot(browser, base: str, shot: str) -> dict:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "/api/cet/" in r.url else None)
    page.goto(base + "/workspace/1/intensive", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1200)
    info = {
        "title": page.title(),
        "marksFilter": page.locator('[data-filter="marks"]').count(),
        "marksChips": page.locator(".sentence-cet-mark").count(),
        "cetRequests": requests,
        "cards": page.locator(".sentence-card").count(),
        "heading": page.locator(".column-intro h2").inner_text(),
    }
    page.screenshot(path=str(EVIDENCE / shot), full_page=True)
    ctx.close()
    return info


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        mine = snapshot(browser, BASE, "08-generic-mode-1440.png")
        baseline = snapshot(browser, BASE_BASELINE, "09-generic-baseline-1440.png")
        browser.close()

    rec("通用模式无卡点 UI / 无 CET 请求（本分支）",
        mine["marksFilter"] == 0 and mine["marksChips"] == 0 and mine["cetRequests"] == [] and mine["cards"] == 32,
        json.dumps(mine, ensure_ascii=False))
    image_mine = Image.open(EVIDENCE / "08-generic-mode-1440.png").convert("RGB")
    image_base = Image.open(EVIDENCE / "09-generic-baseline-1440.png").convert("RGB")
    same_size = image_mine.size == image_base.size
    bbox = ImageChops.difference(image_mine, image_base).getbbox() if same_size else "size-mismatch"
    rec("通用模式与基线 c4a480d 像素级一致", same_size and bbox is None,
        {"mineSize": image_mine.size, "baselineSize": image_base.size, "diffBBox": bbox})
    passed = sum(1 for step in RESULT["steps"] if step["pass"])
    RESULT["summary"] = {"passed": passed, "total": len(RESULT["steps"]), "allPass": passed == len(RESULT["steps"]),
                         "mine": mine, "baseline": baseline}
    (EVIDENCE / "verify_i_generic_result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(RESULT["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
