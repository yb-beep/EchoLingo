#!/usr/bin/env python3
"""四六级听力黑匣子 · 浏览器端到端联调验证（C 负责）。

解决什么问题：
    C 的契约测试走 requests（Python 直连），能证明接口对，
    但证明不了「浏览器里能不能真的跑通」—— 跨端口 CORS、fetch 行为、
    响应在 JS 里的可解析性，这些只有真浏览器能验。

    A 明天开工第一件事就是把页面接到 Mock 上。这个脚本提前把那条路走一遍，
    确认真能通，而不是等 A 卡住了才发现是 C 的 Mock 跨域有问题。

它做什么：
    在**底座页面的浏览器上下文里**（Origin 是 5173）用 fetch 调 5199 的 Mock，
    跑完整链条：材料 → 题目 → 交卷 → 标记 → 确认 → 建练习 → 反馈 → 保存 → 复习。
    全程记录 HTTP 状态与关键字段，任何一步不对就失败。

它证明什么 / 不证明什么：
    ✅ 跨端口调用可行（CORS 配置正确）
    ✅ 响应在浏览器 JS 里可解析，字段符合契约
    ✅ 交卷前不泄露答案、标记保留原始按键时间
    ❌ 不证明 A 的页面写得对（那要 A 交付后验）
    ❌ 不证明真实后端正确（那是 contract 打真实服务的职责）

用法：
    .venv/bin/python scripts/qa/browser_e2e_mock.py
    .venv/bin/python scripts/qa/browser_e2e_mock.py --headed
    .venv/bin/python scripts/qa/browser_e2e_mock.py --json

前置：底座(5173) + Mock(5199) 都在跑；装了 Chrome 或 playwright chromium。
退出码：0 全通｜1 有失败｜2 环境不满足
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BASE = "http://127.0.0.1:5173"
# 同 gen_api_spec：换端口时必须能跟上，否则会连错目标。
MOCK = os.environ.get("CET_MOCK_BASE",
                      f"http://127.0.0.1:{os.environ.get('CET_MOCK_PORT', '5199')}").rstrip("/")

# 在浏览器里跑的完整链条。返回逐步结果，由 Python 侧判定。
FLOW_JS = """
async (mockBase) => {
  const j = async (method, path, body) => {
    const r = await fetch(mockBase + path, {
      method,
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined,
    });
    let d = null, parseErr = null;
    try { d = await r.json(); } catch (e) { parseErr = String(e); }
    return {status: r.status, data: d, parseErr};
  };
  const out = {steps: []};
  const step = (name, ok, info) => out.steps.push({name, ok, info});

  // 0 重置
  await j("POST", "/api/cet/_mock/reset");
  step("重置 Mock 状态", true, {});

  // 1 材料列表
  const m = await j("GET", "/api/cet/materials");
  const mat = m.data && m.data.items && m.data.items[0];
  step("材料列表", m.status === 200 && !!mat,
       {status: m.status, count: m.data && m.data.count});
  if (!mat) return out;

  // 2 题目（交卷前不得含 answer）
  const q = await j("GET", `/api/cet/materials/${mat.materialId}/questions`);
  const qs = (q.data && q.data.items) || [];
  const leak = qs.some(x => "answer" in x || "evidenceRef" in x || "explanation" in x);
  step("题目（交卷前）", q.status === 200 && qs.length > 0 && !leak,
       {status: q.status, count: qs.length, leakedAnswer: leak});
  if (!qs.length) return out;

  // 3 交卷
  const at = await j("POST", "/api/cet/attempts", {
    materialId: mat.materialId,
    materialVersion: mat.materialVersion,
    listeningSessionId: "browser-e2e",
    answers: qs.map(x => ({questionId: x.questionId, choice: 0})),
  });
  const results = (at.data && at.data.results) || [];
  step("交卷并返回答案", at.status === 200 && at.data.answersVisible === true
       && results.length === qs.length,
       {status: at.status, answersVisible: at.data && at.data.answersVisible,
        results: results.length});

  // 4 标记（原始按键秒数必须原样保留）
  const mk = await j("POST", "/api/cet/marks", {
    materialId: mat.materialId,
    materialVersion: mat.materialVersion,
    listeningSessionId: "browser-e2e",
    pressedAtSec: 36.9,
  });
  step("创建标记", mk.status === 200 && !!mk.data.markId
       && Math.abs(mk.data.pressedAtSec - 36.9) < 0.01,
       {status: mk.status, suggested: mk.data && mk.data.suggestedSentenceId,
        pressed: mk.data && mk.data.pressedAtSec});
  if (!mk.data || !mk.data.markId) return out;

  // 5 确认句子
  const cf = await j("PATCH", `/api/cet/marks/${mk.data.markId}`,
                     {confirmedSentenceId: mk.data.suggestedSentenceId});
  step("确认句子", cf.status === 200 && cf.data.confirmedSentenceId === mk.data.suggestedSentenceId,
       {status: cf.status, confirmed: cf.data && cf.data.confirmedSentenceId});

  // 6 建练习
  const pr = await j("POST", "/api/cet/practices", {markId: mk.data.markId});
  step("创建练习", pr.status === 200 && !!pr.data.practiceId,
       {status: pr.status, practiceId: pr.data && pr.data.practiceId,
        sentence: pr.data && pr.data.sentenceId});
  if (!pr.data || !pr.data.practiceId) return out;
  const pid = pr.data.practiceId;

  // 7 没听清 → 应慢放
  const nx = await j("POST", `/api/cet/practices/${pid}/next`, {feedback: "sound"});
  const st = nx.data && nx.data.step;
  step("反馈没听清→慢放", nx.status === 200 && st && st.action === "slow"
       && st.playback && st.playback.speed < 1.0,
       {status: nx.status, action: st && st.action, speed: st && st.playback && st.playback.speed});

  // 8 播放事件（重复上报不重复计时）
  const ev = {eventId: "browser-evt-1", type: "end", stepId: "s1", playedMs: 2000, visible: true};
  const e1 = await j("POST", `/api/cet/practices/${pid}/events`, ev);
  const e2 = await j("POST", `/api/cet/practices/${pid}/events`, ev);
  step("重复事件不重复计时",
       e1.status === 200 && e2.status === 200
       && e1.data.effectiveMs === e2.data.effectiveMs,
       {first: e1.data && e1.data.effectiveMs, second: e2.data && e2.data.effectiveMs});

  // 9 保存卡点
  const fin = await j("POST", `/api/cet/practices/${pid}/finish`, {feedback: "sound"});
  step("保存卡点", fin.status === 200 && fin.data.status === "complete",
       {status: fin.status, state: fin.data && fin.data.status,
        latest: fin.data && fin.data.latestFeedback});

  // 10 复习会话
  const rv = await j("POST", "/api/cet/reviews", {maxItems: 3});
  const items = (rv.data && rv.data.items) || [];
  const boundOk = items.every(i => i.practiceId === pid);
  step("创建复习（绑定原练习 ID）",
       rv.status === 200 && items.length > 0 && boundOk,
       {status: rv.status, items: items.length, boundToOriginal: boundOk});

  // 11 错误形状可解析
  const bad = await j("POST", "/api/cet/attempts", {});
  const bd = bad.data || {};
  step("错误响应可解析",
       bad.status >= 400 && (bd.detail || bd.error_info || bd.error),
       {status: bad.status, shape: bd.error_info ? "error_info" : (bd.detail ? "detail" : "error")});

  return out;
}
"""


def launch(p, headless: bool):
    for name, kw in (
        ("playwright-chromium", {"headless": headless}),
        ("chrome", {"headless": headless, "channel": "chrome"}),
        ("msedge", {"headless": headless, "channel": "msedge"}),
    ):
        try:
            return p.chromium.launch(**kw), name
        except Exception:  # noqa: BLE001
            continue
    return None, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器端到端联调验证（Mock）")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        import urllib.request

        urllib.request.urlopen(f"{MOCK}/api/cet/_mock/info", timeout=4).read()
        urllib.request.urlopen(f"{BASE}/health", timeout=4).read()
    except Exception as exc:  # noqa: BLE001
        print(f"X  服务未就绪：{exc}\n   先跑 scripts/qa/start_cet.sh --mock", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("X  未安装 playwright", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser, channel = launch(p, not args.headed)
        if browser is None:
            print("X  没有可用的浏览器（试过 playwright chromium / Chrome / Edge）", file=sys.stderr)
            return 2
        try:
            page = browser.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            # 关键：在底座页面的 Origin 下执行，形成真实的跨端口调用
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30_000)
            result = page.evaluate(FLOW_JS, MOCK)
        finally:
            browser.close()

    steps = result.get("steps", [])
    failed = [s for s in steps if not s["ok"]]

    if args.json:
        print(json.dumps({"channel": channel, "steps": steps,
                          "jsErrors": js_errors}, ensure_ascii=False, indent=2))
        return 0 if not failed and not js_errors else 1

    print("四六级听力黑匣子 · 浏览器端到端联调（Mock）")
    print(f"底座 {BASE}｜Mock {MOCK}｜浏览器通道 {channel}")
    print("-" * 70)
    for s in steps:
        mark = "OK" if s["ok"] else "X "
        print(f"  [{mark}] {s['name']}")
        if not s["ok"]:
            print(f"        {json.dumps(s['info'], ensure_ascii=False)}")
    print("-" * 70)
    if js_errors:
        print(f"JS 运行时错误 {len(js_errors)} 条：")
        for e in js_errors[:5]:
            print(f"  ! {e[:120]}")
    print(f"结论：{len(steps) - len(failed)}/{len(steps)} 步通过"
          + ("" if not failed else f"，失败 {len(failed)} 步"))
    if not failed and not js_errors:
        print("\nA 可以按这个方式把页面接到 Mock：API base 指向 " + MOCK)
    return 0 if not failed and not js_errors else 1


if __name__ == "__main__":
    sys.exit(main())
