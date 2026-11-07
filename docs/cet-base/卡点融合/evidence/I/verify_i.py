"""包 I 浏览器验证：逐句精听页的卡点标记 / 我的卡点筛选 / 真实媒体播放。

前置：真实服务（默认 http://127.0.0.1:5304，CET 模式 + CET_LISTENING_AGENT=1）。
用法：.venv/bin/python docs/cet-base/卡点融合/evidence/I/verify_i.py
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:5304")
SESSION_ID = "cet-session-i-test"
EVIDENCE = Path(__file__).resolve().parent
RESULT: dict = {"base": BASE, "sessionId": SESSION_ID, "steps": []}


def rec(name: str, ok: bool, actual) -> None:
    RESULT["steps"].append({"step": name, "pass": bool(ok), "actual": actual})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual}")


def http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def ensure_fixture_marks() -> dict:
    """确保演示会话里有两条卡点：一条待确认、一条待练（确认需先交卷）。"""
    status, material = http_json("GET", "/api/cet/materials/by-lesson/1")
    assert status == 200, material
    material_id = material["materialId"]
    material_version = material["materialVersion"]
    status, payload = http_json("GET", f"/api/cet/marks?materialId={urllib.parse.quote(material_id)}&listeningSessionId={SESSION_ID}")
    assert status == 200, payload
    marks = payload.get("items", [])
    if len(marks) < 2:
        for seconds in (5.0, 32.0):
            status, created = http_json("POST", "/api/cet/marks", {
                "materialId": material_id,
                "materialVersion": material_version,
                "listeningSessionId": SESSION_ID,
                "pressedAtSec": seconds,
                "reactionDelaySeconds": 2.5,
                "speed": 1.0,
            })
            assert status in (200, 201), created
        http_json("POST", "/api/cet/attempts", {
            "materialId": material_id,
            "materialVersion": material_version,
            "listeningSessionId": SESSION_ID,
            "answers": [],
        })
        status, payload = http_json("GET", f"/api/cet/marks?materialId={urllib.parse.quote(material_id)}&listeningSessionId={SESSION_ID}")
        marks = payload.get("items", [])
    # 把第二条确认到 suggestedSentenceId，制造「待练」态，便于对照工作区的 blue badge。
    target = next((m for m in marks if float(m.get("pressedAtSec") or 0) > 20), marks[-1])
    if target and not target.get("confirmedSentenceId") and target.get("suggestedSentenceId"):
        http_json("PATCH", f"/api/cet/marks/{target['markId']}", {"confirmedSentenceId": target["suggestedSentenceId"]})
        status, payload = http_json("GET", f"/api/cet/marks?materialId={urllib.parse.quote(material_id)}&listeningSessionId={SESSION_ID}")
        marks = payload.get("items", [])
    return {"material": material, "marks": marks}


def chip_details(page) -> list[dict]:
    return page.evaluate("""
      () => [...document.querySelectorAll('.sentence-cet-mark')].map(chip => {
        const card = chip.closest('.sentence-card');
        const style = getComputedStyle(chip);
        return {
          key: Number(card?.dataset.key),
          text: chip.textContent.trim(),
          backgroundColor: style.backgroundColor,
          color: style.color,
          borderLeftColor: style.borderLeftColor,
          sentence: card?.querySelector('.sentence-copy')?.textContent.trim().slice(0, 60),
        };
      })
    """)


def run_desktop(browser) -> None:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.add_init_script(f"localStorage.setItem('cet.session.v1','{SESSION_ID}')")
    page.goto(BASE + "/workspace/1/intensive", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1200)

    rec("I2 筛选按钮存在且名称正确", page.locator('[data-filter="marks"]').count() == 1,
        page.locator('[data-filter="marks"]').inner_text() if page.locator('[data-filter="marks"]').count() else "missing")
    rec("I2 筛选样式复用 .mode-filter", page.evaluate("()=>document.querySelector('[data-filter=marks]').classList.contains('mode-filter')"), True)

    chips = chip_details(page)
    rec("I1 卡点句出现「卡点」标记", len(chips) == 2, json.dumps(chips, ensure_ascii=False))
    rec("I1 待确认态颜色/文案与工作区一致",
        any(c["text"] == "卡点 · 待确认" and c["backgroundColor"] == "rgb(253, 235, 200)" and c["color"] == "rgb(138, 90, 0)" for c in chips),
        json.dumps([c for c in chips if "待确认" in c["text"]], ensure_ascii=False))
    rec("I1 待练态颜色/文案与工作区一致",
        any(c["text"] == "卡点 · 待练" and c["backgroundColor"] == "rgb(216, 230, 248)" and c["color"] == "rgb(23, 69, 122)" for c in chips),
        json.dumps([c for c in chips if "待练" in c["text"]], ensure_ascii=False))
    page.screenshot(path=str(EVIDENCE / "01-marks-all-1440.png"), full_page=True)

    # 点击「我的卡点」：只保留有卡点的句子
    page.click('[data-filter="marks"]')
    page.wait_for_timeout(500)
    keys = page.eval_on_selector_all(".sentence-card", "els=>els.map(e=>Number(e.dataset.key))")
    rec("I2 点击「我的卡点」只显示卡点句", sorted(keys) == [0, 10], keys)
    rec("I2 我的卡点计数", page.locator("#count-marks").inner_text() == "2", page.locator("#count-marks").inner_text())
    rec("I2 结果统计同步", page.locator("#study-progress").inner_text() == "显示 2 / 32 句", page.locator("#study-progress").inner_text())
    page.screenshot(path=str(EVIDENCE / "02-marks-filter-1440.png"), full_page=True)

    # 真实媒体：点击卡点句「原声整句」，读 currentTime / paused / playbackRate
    page.click('.sentence-card[data-key="0"] .sentence-header-play')
    page.wait_for_timeout(900)
    media = page.evaluate("""()=>{const a=document.querySelector('#intensive-audio');return {
        currentTime:+a.currentTime.toFixed(3), paused:a.paused, playbackRate:a.playbackRate,
        src:a.currentSrc.split('/').slice(-2).join('/'), duration:+a.duration.toFixed(2)};}""")
    rec("真实媒体播放（原声整句）", 0 < media["currentTime"] < 4 and media["paused"] is False and media["src"].endswith(".mp3"), json.dumps(media))
    page.evaluate("()=>document.querySelector('#intensive-audio').pause()")

    # 展开句子 → 单句 1.25× → 再播，读 playbackRate
    page.click('.sentence-card[data-key="0"] .sentence-head')
    page.wait_for_timeout(300)
    page.click('.sentence-card[data-key="0"] [data-sentence-speed="1.25"]')
    page.click('.sentence-card[data-key="0"] .sentence-play')
    page.wait_for_timeout(600)
    rate = page.evaluate("()=>{const a=document.querySelector('#intensive-audio');return {playbackRate:a.playbackRate, paused:a.paused, currentTime:+a.currentTime.toFixed(3)};}")
    rec("真实媒体倍速（1.25×）", abs(rate["playbackRate"] - 1.25) < 0.001 and rate["paused"] is False, json.dumps(rate))
    page.evaluate("()=>document.querySelector('#intensive-audio').pause()")

    rec("页面无 JS 错误", not errors, errors)
    RESULT["desktop"] = {"chips": chips, "media": media, "rate": rate, "errors": errors}
    ctx.close()


def run_mobile(browser) -> None:
    ctx = browser.new_context(viewport={"width": 375, "height": 812})
    page = ctx.new_page()
    page.add_init_script(f"localStorage.setItem('cet.session.v1','{SESSION_ID}')")
    page.goto(BASE + "/workspace/1/intensive", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)
    rec("375 宽度筛选按钮可见", page.locator('[data-filter="marks"]').is_visible(), page.locator('[data-filter="marks"]').inner_text())
    page.screenshot(path=str(EVIDENCE / "03-marks-all-375.png"), full_page=True)
    page.click('[data-filter="marks"]')
    page.wait_for_timeout(400)
    rec("375 宽度卡点筛选结果", page.locator(".sentence-card").count() == 2, page.locator(".sentence-card").count())
    page.screenshot(path=str(EVIDENCE / "04-marks-filter-375.png"), full_page=True)
    ctx.close()


def run_workspace_compare(browser) -> None:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.add_init_script(f"localStorage.setItem('cet.session.v1','{SESSION_ID}')")
    page.goto(BASE + "/workspace/1", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2200)
    text = page.locator("#cet-marks").inner_text() if page.locator("#cet-marks").count() else ""
    rec("工作区卡点面板可对照（待确认/待练）", "卡点标记" in text and "待确认" in text and "待练" in text, text[:200])
    page.screenshot(path=str(EVIDENCE / "05-workspace-marks-1440.png"), full_page=False)
    ctx.close()


def main() -> None:
    fixture = ensure_fixture_marks()
    RESULT["fixture"] = {
        "materialId": fixture["material"]["materialId"],
        "marks": [{k: m.get(k) for k in ("markId", "pressedAtSec", "suggestedSentenceId", "confirmedSentenceId")} for m in fixture["marks"]],
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        run_desktop(browser)
        run_mobile(browser)
        run_workspace_compare(browser)
        browser.close()
    passed = sum(1 for step in RESULT["steps"] if step["pass"])
    RESULT["summary"] = {"passed": passed, "total": len(RESULT["steps"]), "allPass": passed == len(RESULT["steps"])}
    (EVIDENCE / "verify_i_result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(RESULT["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
