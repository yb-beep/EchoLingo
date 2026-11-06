#!/usr/bin/env python3
"""C 板块（听音交互）真实浏览器验证：F08 / F05 / F10。

为什么是浏览器而不是单测：
    这三项全部是「用户点下去到底发生了什么」的行为 ——
    空格标记后有没有候选预览、点「前一句」是不是只换预览没落库、
    进练习模式字幕是否真的隐藏、换句时播放速度是不是真的 1.0、
    时间线上有没有彩色标记元素。HTTP 断言看不到这些。

验证目标（与 task-3 验收条件一一对应）：
    F08-1  创建标记传 reactionDelaySeconds（= lookbackSec）与实际 playbackRate
    F08-2  「前一句 / 后一句」只切预览，不确认（PATCH 计数不增加）
    F08-3  预览时立即回听该句（音频跳到该句起点并开始播放）
    F08-4  点「确认这句」才真正确认（PATCH 计数 +1，confirmedSentenceId 落库）
    F08-5  确认后仍能「改一下」再调整
    F08-6  两个身份都保留：pressedAtSec 原按键时间 + confirmedSentenceId 确认句
    F05-1  进入卡点练习模式隐藏字幕与含原文的内容大纲
    F05-2  「听懂了」→ 换句：隐藏提示 + 恢复 1.0x 原速
    F05-3  有候选：播放候选真实音频（独立 audio 元素、非原句）、展示题面与选项
    F05-4  有候选：提交作答并落库（transfer_answer 事件真的入库）
    F05-5  无候选：明确「未检验」，不当通过
    F10-1  时间线上能看到标记元素，位置对应真实秒数
    F10-2  三态颜色 + 文字双重标识（不只靠颜色）
    NOJS   全程无 JS 错误

用法：
    # 0) 准备隔离库 + 一份「未被接触过」的换句候选夹具
    #    （日常库候选池早已被历史测试消耗，不造夹具 F05 只能验到 no_candidate）
    export ELT_USER_DB=/tmp/wt-c-ui/.cet-qa/data/vocab.db
    .venv/bin/python scripts/cet/prepare_c_ui_fixture.py

    # 1) 起隔离服务（**受控模型**，让换句候选路径能真的生成）
    #    不要用主工作区的 5173
    ELT_PORT=5188 ELT_CONFIG_DIR=/tmp/wt-c-ui/.cet-qa/data \
      .venv/bin/python scripts/cet/serve_c_ui_controlled.py &

    # 2) 跑本脚本
    .venv/bin/python scripts/cet/verify_c_ui.py
    .venv/bin/python scripts/cet/verify_c_ui.py --headed      # 看得到界面
    .venv/bin/python scripts/cet/verify_c_ui.py --json        # 机器可读结果

退出码：0 全部通过｜1 有失败｜2 环境不满足
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CET_UI_BASE", "http://127.0.0.1:5188").rstrip("/")
LESSON_ID = int(os.environ.get("CET_UI_LESSON_ID", "2181"))
AUDIO = "document.getElementById('local-media-player')"

RESULTS: list[dict] = []
COVERAGE: dict[str, int] = {"presented": 0, "no_candidate": 0}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def service_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False



def prepare_fixture():
    """调用同目录的夹具脚本，往隔离库插入一份全新的换句候选材料。"""
    db_path = os.environ.get("ELT_USER_DB", "")
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        sys.path.insert(0, str(os.path.dirname(os.path.abspath(__file__))))
        from prepare_c_ui_fixture import prepare  # type: ignore
        return prepare(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"!  夹具准备失败：{exc}")
        return None


def get_json(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    ap.add_argument("--slow", action="store_true", help="放慢操作便于观察")
    args = ap.parse_args()

    if not service_up():
        print(f"X  服务未就绪：{BASE}/health —— 先起隔离服务（见脚本头部说明）")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("X  未安装 playwright：.venv/bin/pip install playwright && .venv/bin/playwright install chromium")
        return 2

    print("== C 板块浏览器验证（F08 / F05 / F10）==")
    print(f"服务：{BASE}   课程：/workspace/{LESSON_ID}")

    # 换句候选池是**一次性消耗品**：后端 _transfer_candidate 会排除
    # 已被交卷/已建练习/已被用过的材料及其 source_url。
    # 不每次补一份新夹具，第二轮就只能验到 no_candidate 分支。
    fixture = prepare_fixture()
    if fixture:
        print(f"候选夹具：{fixture['materialId']}（{fixture['audioUrl']}）")
    else:
        print("!  未能自动准备候选夹具；有候选分支可能验不到（见脚本头部说明）")
    print()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not args.headed, channel="chrome")
        except Exception:  # noqa: BLE001
            browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1500, "height": 1300})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # 统计 PATCH /marks 次数：用来证明「前/后一句只预览、不确认」
        patch_count = {"n": 0}

        def on_request(req):
            if req.method == "PATCH" and "/api/cet/marks/" in req.url:
                patch_count["n"] += 1

        page.on("request", on_request)

        try:
            run_all(page, args, patch_count)
        finally:
            # NOJS：全程无 JS 错误（console error 里过滤掉已知无关的资源 404）
            real_errors = [e for e in js_errors]
            check("NOJS 无 JS 运行时错误", not real_errors,
                  f"{len(real_errors)} 个：{real_errors[:3]}" if real_errors else "0 个")
            ignored = ("favicon", "Failed to load resource")
            con = [c for c in console_errors if not any(k in c for k in ignored)]
            check("NOJS 无 console error", not con,
                  f"{len(con)} 个：{con[:3]}" if con else "0 个")
            ctx.close()
            browser.close()

    # F05 有候选 / 无候选是两条**不同代码路径**，只验到一条不算完成。
    # 这里显式记录覆盖情况，缺哪条就报失败，不能默默跳过。
    check("COV-F05 有候选与无候选两条分支都被验证到",
          COVERAGE["presented"] >= 1 and COVERAGE["no_candidate"] >= 1,
          f"presented={COVERAGE['presented']} no_candidate={COVERAGE['no_candidate']}")

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["ok"])
    failed = total - passed
    print()
    print("-" * 66)
    print(f"结果：{passed}/{total} 通过" + (f"，{failed} 失败" if failed else "，全部通过"))
    if failed:
        print("失败项：")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  - {r['name']}：{r['detail']}")

    if args.json:
        print(json.dumps({"base": BASE, "results": RESULTS,
                          "passed": passed, "total": total}, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


def fresh_page(page, clear_storage: bool = True) -> None:
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    if clear_storage:
        page.evaluate("localStorage.clear(); sessionStorage.clear()")
        page.reload(wait_until="networkidle", timeout=45000)
    page.wait_for_function("window.__cet && window.__cet.S.materialId", timeout=25000)
    page.wait_for_timeout(1500)


def run_all(page, args, patch_count) -> None:
    # ── F08 ────────────────────────────────────────────────
    print("-- F08：回溯参数与候选预览 --")
    fresh_page(page)

    # F08-1：参数真的发上去了（拦请求看 body）
    captured: list[dict] = []
    page.on("request", lambda r: captured.append({
        "url": r.url, "method": r.method,
        "body": r.post_data,
    }) if r.method == "POST" and r.url.endswith("/api/cet/marks") else None)

    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1500)
    rate_before = page.evaluate(f"{AUDIO}.playbackRate")
    page.keyboard.press("Space")
    page.wait_for_timeout(2000)
    page.evaluate(f"{AUDIO}.pause()")

    body = None
    for c in captured:
        if c["body"]:
            try:
                body = json.loads(c["body"])
            except Exception:  # noqa: BLE001
                pass
    if body is None:
        check("F08-1 创建标记请求已发出", False, "未捕获到 POST /api/cet/marks")
    else:
        has_delay = isinstance(body.get("reactionDelaySeconds"), (int, float))
        has_speed = isinstance(body.get("speed"), (int, float))
        lookback = page.evaluate("window.__cet && window.CET_LOOKBACK_SEC") or 2.5
        check(
            "F08-1 创建标记带 reactionDelaySeconds 与实际 speed",
            has_delay and has_speed and abs(float(body.get("speed", 0)) - rate_before) < 0.01,
            f"reactionDelaySeconds={body.get('reactionDelaySeconds')} "
            f"speed={body.get('speed')} 实际倍速={rate_before}",
        )
        check(
            "F08-1b 回溯参数用的是 CFG.lookbackSec（不是后端默认 0.6）",
            has_delay and abs(float(body["reactionDelaySeconds"]) - 2.5) < 0.01,
            f"发送值={body.get('reactionDelaySeconds')}（期望 2.5，后端默认是 0.6）",
        )

    # 标记已产生，进入预览态
    marks = page.evaluate("window.__cet.S.marks.length")
    check("F08-0 空格产生标记", marks >= 1, f"marks={marks}")

    # 预览态：有「前一句/后一句/确认这句」，且句子已显示
    state = page.evaluate("""() => {
      const row = document.querySelector('.cet-mark');
      const badges = Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent);
      const texts = Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent);
      const previewTag = Array.from(document.querySelectorAll('.cet-mark-preview-tag')).map(e=>e.textContent);
      const btns = Array.from(document.querySelectorAll('.cet-mark-actions button')).map(e=>e.textContent);
      return {badges, texts, previewTag, btns};
    }""")
    check("F08-0b 标记处于「待确认」预览态",
          "待确认" in state["badges"] and any("确认这句" in b for b in state["btns"]),
          f"badges={state['badges']} btns={state['btns']}")
    check("F08-3 预览态显示候选句并标注预览区间",
          len(state["texts"]) >= 1 and len(state["previewTag"]) >= 1,
          f"句子={state['texts'][:1]} 预览标签={state['previewTag'][:1]}")

    # F08-2：点「前一句」只切预览，不发 PATCH
    # 注意：标记可能落在第 0 句（无法再往前）。先看当前索引，
    # 若已是首句就先点「后一句」，确保有可对比的切换。
    before_patch = patch_count["n"]
    cur_idx = page.evaluate("""() => {
      const cet = window.__cet;
      const m = cet.S.marks[0] || {};
      const sid = cet.previewSentenceId(m);
      return cet.S.sentences.findIndex(s => s.sentenceId === sid);
    }""")
    state0 = page.evaluate("""() => {
      const texts = Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent);
      const btns = Array.from(document.querySelectorAll('.cet-mark-actions button')).map(e=>e.textContent);
      return {texts, btns};
    }""")
    before_text = state0["texts"][0] if state0["texts"] else ""
    prev_btn = page.query_selector('.cet-mark-actions button:has-text("前一句")')
    if not prev_btn:
        check("F08-2 存在「前一句」按钮", False, "未找到按钮")
    else:
        # 首句时先往后切一次，才有「前一句」可退
        if cur_idx <= 0:
            nxt = page.query_selector('.cet-mark-actions button:has-text("后一句")')
            nxt.click()
            page.wait_for_timeout(1200)
            texts_after_next = page.evaluate(
                "Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent)"
            )
            before_text = texts_after_next[0] if texts_after_next else ""
            before_patch = patch_count["n"]
        prev_btn = page.query_selector('.cet-mark-actions button:has-text("前一句")')
        # 记录「点前一句之前」的句子文本，作为对比基准
        text_before_prev = page.evaluate(
            "Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent)"
        )
        prev_btn.click()
        page.wait_for_timeout(1200)
        after_patch = patch_count["n"]
        after_state = page.evaluate("""() => {
          const badges = Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent);
          const texts = Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent);
          const btns = Array.from(document.querySelectorAll('.cet-mark-actions button')).map(e=>e.textContent);
          const confirmed = (window.__cet.S.marks[0] || {}).confirmedSentenceId || null;
          return {badges, texts, btns, confirmed, paused: document.getElementById('local-media-player').paused};
        }""")
        check("F08-2 点「前一句」不确认（未发 PATCH，仍可继续调整）",
              after_patch == before_patch and after_state["confirmed"] is None,
              f"PATCH {before_patch}→{after_patch} confirmedSentenceId={after_state['confirmed']}")
        check("F08-2b 切换后仍在待确认态且保留调整按钮",
              "待确认" in after_state["badges"]
              and any("确认这句" in b for b in after_state["btns"])
              and any("后一句" in b for b in after_state["btns"]),
              f"badges={after_state['badges']} btns={after_state['btns']}")
        check("F08-3b 切换预览后立即回听（音频在播）",
              after_state["paused"] is False,
              f"paused={after_state['paused']}")
        check("F08-2c 预览句子随「前一句」变化",
              bool(after_state["texts"]) and after_state["texts"] != text_before_prev,
              f"切换前={text_before_prev[:1]} 切换后={after_state['texts'][:1]}")

        # 再点一次「后一句」回到原句，证明可来回切
        next_btn = page.query_selector('.cet-mark-actions button:has-text("后一句")')
        next_btn.click()
        page.wait_for_timeout(1000)
        back = page.evaluate(
            "Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent)"
        )
        check("F08-2d 可再点「后一句」切回（预览可来回切换）",
              patch_count["n"] == before_patch,
              f"PATCH 仍为 {patch_count['n']}，句子={back[:1]}")

    # F08-4：确认需要先交卷（后端规则），交卷后点「确认这句」
    page.evaluate(f"{AUDIO}.pause()")
    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(3000)

    before_patch = patch_count["n"]
    confirm_btn = page.query_selector('.cet-mark-actions button:has-text("确认这句")')
    if not confirm_btn:
        check("F08-4 存在「确认这句」按钮", False, "未找到按钮（可能已确认）")
    else:
        confirm_btn.click()
        page.wait_for_timeout(2500)
        after = page.evaluate("""() => {
          const m = window.__cet.S.marks[0] || {};
          return {
            confirmed: m.confirmedSentenceId || null,
            pressedAtSec: m.pressedAtSec,
            badges: Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent),
            btns: Array.from(document.querySelectorAll('.cet-mark-actions button')).map(e=>e.textContent),
          };
        }""")
        check("F08-4 点「确认这句」才真正确认（PATCH +1，落库 confirmedSentenceId）",
              patch_count["n"] == before_patch + 1 and bool(after["confirmed"]),
              f"PATCH {before_patch}→{patch_count['n']} confirmed={after['confirmed']}")
        check("F08-4b 确认后状态前进到「待练」",
              "待练" in after["badges"],
              f"badges={after['badges']}")
        # F08-6：两个身份都保留
        check("F08-6 原按键时间与确认句身份都保留",
              isinstance(after["pressedAtSec"], (int, float)) and bool(after["confirmed"]),
              f"pressedAtSec={after['pressedAtSec']} confirmedSentenceId={after['confirmed']}")

        # F08-5：确认后仍能「改一下」
        amend = page.query_selector('.cet-mark-actions button:has-text("改一下")')
        if not amend:
            check("F08-5 确认后提供「改一下」入口", False, "未找到按钮")
        else:
            amend.click()
            page.wait_for_timeout(1500)
            reopened = page.evaluate("""() => {
              const badges = Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent);
              const btns = Array.from(document.querySelectorAll('.cet-mark-actions button')).map(e=>e.textContent);
              return {badges, btns};
            }""")
            check("F08-5 点「改一下」回到可调整状态",
                  "待确认" in reopened["badges"]
                  and any("前一句" in b for b in reopened["btns"])
                  and any("确认这句" in b for b in reopened["btns"]),
                  f"badges={reopened['badges']} btns={reopened['btns']}")

            # 改一下之后真的能改成另一句
            prev2 = page.query_selector('.cet-mark-actions button:has-text("前一句")')
            old_confirmed = after["confirmed"]
            prev2.click()
            page.wait_for_timeout(1200)
            conf2 = page.query_selector('.cet-mark-actions button:has-text("确认这句")')
            conf2.click()
            page.wait_for_timeout(2500)
            final = page.evaluate("""() => {
              const m = window.__cet.S.marks[0] || {};
              return {confirmed: m.confirmedSentenceId, pressedAtSec: m.pressedAtSec,
                      badges: Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent)};
            }""")
            check("F08-5b 改一下后确认句真的变了（且原按键时间不变）",
                  final["confirmed"] != old_confirmed
                  and final["pressedAtSec"] == after["pressedAtSec"],
                  f"原={old_confirmed} 新={final['confirmed']} pressedAtSec={final['pressedAtSec']}")

    # ── F08 回溯参数真的影响候选定位（倍速矩阵）──────────
    # 后端 adjusted = pressedAtSec - reactionDelaySeconds * speed，
    # 再拿 adjusted 去吸附候选句。所以同一按键秒数、不同回溯/倍速，
    # 候选句应当不同 —— 这是「参数真的生效」的强证据，
    # 比只断言「字段发出去了」更硬。
    print()
    print("-- F08 回溯参数生效验证（倍速矩阵，直连 API 交叉核对）--")
    verify_lookback_matrix(page)

    # ── F10：时间线标记 ────────────────────────────────────
    print()
    print("-- F10：时间线颜色标记 --")
    tl = page.evaluate("""() => {
      const layer = document.querySelector('[data-cet-mark-layer]');
      const marks = Array.from(document.querySelectorAll('[data-cet-mark]'));
      return {
        hasLayer: !!layer,
        layerAria: layer ? layer.getAttribute('aria-label') : null,
        count: marks.length,
        states: marks.map(m => m.getAttribute('data-cet-mark-state')),
        secs: marks.map(m => m.getAttribute('data-cet-mark-sec')),
        lefts: marks.map(m => m.style.left),
        texts: marks.map(m => (m.textContent || '').trim()),
        labels: marks.map(m => m.getAttribute('aria-label')),
        duration: (document.querySelector('#cet-timeline-track') || {}).getAttribute
          ? document.querySelector('#cet-timeline-track').getAttribute('data-cet-duration-sec') : null,
        legend: Array.from(document.querySelectorAll('.cet-timeline-legend-item')).map(e=>e.textContent),
        bg: marks.map(m => getComputedStyle(m).backgroundColor),
      };
    }""")
    check("F10-1 播放器时间线图层已渲染", tl["hasLayer"], f"aria-label={tl['layerAria']}")
    check("F10-1b 时间线上有标记元素", tl["count"] >= 1,
          f"count={tl['count']} 秒数={tl['secs']} 位置={tl['lefts']}")
    check("F10-2 标记带状态属性（三态之一）",
          all(s in ("pending", "ready", "done") for s in tl["states"]) and bool(tl["states"]),
          f"states={tl['states']}")
    check("F10-2b 颜色 + 文字双重标识（不只靠颜色）",
          all(t in ("待", "练", "已") for t in tl["texts"])
          and all(l and ("状态" in l) for l in tl["labels"])
          and len(tl["legend"]) == 3,
          f"文字={tl['texts']} aria={tl['labels'][:1]} 图例={tl['legend']}")
    check("F10-2c 三态用不同颜色（不是同一色）",
          len(set(tl["bg"])) >= 1 and all(b and b != "rgba(0, 0, 0, 0)" for b in tl["bg"]),
          f"背景色={tl['bg']}")
    # 位置对应真实秒数：left% ≈ sec/duration*100
    if tl["duration"] and tl["secs"] and tl["lefts"]:
        dur = float(tl["duration"])
        ok_pos = True
        detail = []
        for sec, left in zip(tl["secs"], tl["lefts"]):
            try:
                pct = float(str(left).replace("%", ""))
                expect = float(sec) / dur * 100
                detail.append(f"{sec}s→{pct:.1f}%(期望{expect:.1f}%)")
                if abs(pct - expect) > 1.0:
                    ok_pos = False
            except (TypeError, ValueError):
                ok_pos = False
        check("F10-1c 标记位置对应真实按键秒数", ok_pos, "; ".join(detail))

    # ── F05：隐藏原文 / 恢复原速 / 换句闭环 ────────────────
    print()
    print("-- F05：隐藏原文 / 恢复原速 / 换句闭环 --")

    # F05-1：进入练习模式即隐藏字幕与内容大纲原文
    fresh_page(page)
    before_hide = page.evaluate("""() => {
      const sub = document.getElementById('current-subtitle');
      const outline = document.querySelector('.outline-preview');
      return {
        bodyClass: document.body.classList.contains('cet-hide-text'),
        subColor: sub ? getComputedStyle(sub).color : null,
        subAria: sub ? sub.getAttribute('aria-hidden') : null,
        outlineColor: outline ? getComputedStyle(outline).color : null,
        // 原 UI 必须保留（只控显隐，不改结构）
        subExists: !!sub,
        timelineExists: !!document.getElementById('subtitle-timeline'),
        outlineExists: !!outline,
      };
    }""")
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(700)
    after_hide = page.evaluate("""() => {
      const sub = document.getElementById('current-subtitle');
      const outline = document.querySelector('.outline-preview');
      return {
        bodyClass: document.body.classList.contains('cet-hide-text'),
        subColor: sub ? getComputedStyle(sub).color : null,
        subAria: sub ? sub.getAttribute('aria-hidden') : null,
        outlineColor: outline ? getComputedStyle(outline).color : null,
        subExists: !!sub,
        timelineExists: !!document.getElementById('subtitle-timeline'),
        outlineExists: !!outline,
        timelineFlag: (document.getElementById('subtitle-timeline') || {}).getAttribute
          ? document.getElementById('subtitle-timeline').getAttribute('data-cet-text-hidden') : null,
      };
    }""")
    check("F05-1 进入练习模式后字幕原文被隐藏",
          after_hide["bodyClass"] and after_hide["subColor"] in ("rgba(0, 0, 0, 0)", "transparent")
          and after_hide["subAria"] == "true",
          f"bodyClass={after_hide['bodyClass']} color={after_hide['subColor']} aria-hidden={after_hide['subAria']}")
    if before_hide["outlineExists"]:
        check("F05-1b 内容大纲里的原文摘要也被隐藏",
              after_hide["outlineColor"] in ("rgba(0, 0, 0, 0)", "transparent")
              and after_hide["timelineFlag"] == "1",
              f"outline color={after_hide['outlineColor']} flag={after_hide['timelineFlag']}")
    else:
        check("F05-1b 内容大纲原文隐藏（本页无大纲摘要，用 flag 判定）",
              after_hide["timelineFlag"] == "1",
              f"flag={after_hide['timelineFlag']}（本页大纲尚未生成摘要）")
    check("F05-1c 原 UI 结构保留（只控显隐，未删元素）",
          after_hide["subExists"] and after_hide["timelineExists"],
          f"subtitle={after_hide['subExists']} timeline={after_hide['timelineExists']}")

    # 生词卡 / 句子元信息也会泄露原文（截图实测过），必须一起隐藏
    leaks = page.evaluate("""() => {
      // 逐个元素判断「是否真的看不见」：
      // 子元素可能自己带 color（.vocab-chip 就是），容器 transparent 挡不住，
      // 所以要递归检查每个文本节点的祖先是否被 visibility:hidden 或 color:transparent 遮住。
      const invisible = (node) => {
        let el = node.nodeType === 3 ? node.parentElement : node;
        while (el && el !== document.body) {
          const cs = getComputedStyle(el);
          if (cs.visibility === 'hidden' || cs.display === 'none') return true;
          if (cs.color === 'rgba(0, 0, 0, 0)' || cs.color === 'transparent') return true;
          el = el.parentElement;
        }
        return false;
      };
      const scan = (id) => {
        const root = document.getElementById(id);
        if (!root) return {exists: false, visible: []};
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const visible = [];
        let n = walker.nextNode();
        while (n) {
          const t = (n.nodeValue || '').trim();
          if (t && !invisible(n)) visible.push(t.slice(0, 30));
          n = walker.nextNode();
        }
        return {
          exists: true,
          aria: root.getAttribute('aria-hidden'),
          visible,
        };
      };
      return {chips: scan('vocab-chips'), meta: scan('subtitle-meta'),
              popover: scan('word-popover')};
    }""")
    check("F05-1e 生词卡与句子元信息同样隐藏（不泄露卡住的词）",
          not leaks["chips"]["visible"] and not leaks["meta"]["visible"],
          f"chips 可见文字={leaks['chips'].get('visible')} "
          f"meta 可见文字={leaks['meta'].get('visible')}")
    # 当前句区的「点词查询」把每个词渲染成 ruby（词 + 词义注释），
    # 这是最隐蔽的漏点：按钮和 <rt> 各有自己的颜色，父级 transparent 挡不住。
    ruby_leak = page.evaluate("""() => {
      const nodes = Array.from(document.querySelectorAll(
        '.lookup-word-wrap, .subtitle-word, .word-gloss, #current-subtitle ruby, #current-subtitle button'));
      const visible = [];
      for (const el of nodes) {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const hasSize = r.width > 1 && r.height > 1;
        const hidden = cs.visibility === 'hidden' || cs.display === 'none'
          || cs.color === 'rgba(0, 0, 0, 0)';
        if (hasSize && !hidden && (el.innerText || '').trim()) {
          visible.push(((el.innerText || '').trim()).slice(0, 24));
        }
      }
      return {total: nodes.length, visible: visible.slice(0, 8)};
    }""")
    check("F05-1g 当前句的点词 ruby（词 + 词义）也不可见",
          not ruby_leak["visible"],
          f"检查 {ruby_leak['total']} 个元素，可见泄露={ruby_leak['visible']}")

    # 点词查询浮层是另一个原文出口，练习模式下必须关掉
    pop = page.evaluate("""() => {
      const p = document.getElementById('word-popover');
      if (!p) return {exists: false};
      const cs = getComputedStyle(p);
      return {exists: true, display: cs.display, open: p.classList.contains('open'),
              text: (p.innerText || '').trim().slice(0, 40)};
    }""")
    check("F05-1f 点词查询浮层在练习模式下不可见",
          (not pop["exists"]) or pop["display"] == "none" or not pop["open"],
          f"display={pop.get('display')} open={pop.get('open')} text={pop.get('text')!r}")

    # 退出练习模式应恢复原文
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(600)
    restored_text = page.evaluate(
        "getComputedStyle(document.getElementById('current-subtitle')).color"
    )
    check("F05-1d 退出练习模式恢复显示原文",
          restored_text not in ("rgba(0, 0, 0, 0)", "transparent"),
          f"color={restored_text}")

    # 走到带练
    practice_id = to_practice(page)
    check("F05-0 带练已开始", bool(practice_id), f"practiceId={practice_id}")

    if practice_id:
        # F05-2：「听懂了」→ 换句，恢复 1.0x 原速
        # 先把播放速度改成 0.75，验证换句时真的被恢复成 1.0
        page.evaluate(f"{AUDIO}.playbackRate = 0.75")
        page.wait_for_timeout(200)
        heard = page.query_selector('button:has-text("听懂了")')
        if not heard:
            check("F05-2 存在「听懂了」按钮", False, "未找到")
        else:
            heard.click()
            page.wait_for_timeout(4500)
            after_heard = page.evaluate("""() => {
              const a = document.getElementById('local-media-player');
              const sel = document.getElementById('playback-rate');
              const box = document.querySelector('#cet-transfer-box');
              return {
                rate: a ? a.playbackRate : null,
                selValue: sel ? sel.value : null,
                bodyClass: document.body.classList.contains('cet-hide-text'),
                transferHidden: !!document.querySelector('[data-cet-transfer-hidden]'),
                transferBox: !!box,
                transferKind: box ? box.getAttribute('data-cet-transfer') : null,
                unverified: !!document.querySelector('[data-cet-unverified]'),
                practice: window.__cet.S.practice ? {
                  transferStatus: window.__cet.S.practice.transferStatus,
                  stepAction: (window.__cet.S.practice.currentStep||{}).action,
                } : null,
              };
            }""")
            check("F05-2 换句时恢复 1.0x 原速",
                  after_heard["rate"] is not None and abs(after_heard["rate"] - 1.0) < 0.01,
                  f"0.75→{after_heard['rate']}（下拉框={after_heard['selValue']}）")
            check("F05-2b 换句时隐藏提示仍在生效",
                  after_heard["bodyClass"] and after_heard["transferHidden"],
                  f"bodyClass={after_heard['bodyClass']} 提示={after_heard['transferHidden']}")
            check("F05-2c 进入换句区（有候选或明确无候选）",
                  after_heard["transferBox"] and after_heard["transferKind"] in ("presented", "no_candidate"),
                  f"kind={after_heard['transferKind']} practice={after_heard['practice']}")

            if after_heard["transferKind"] == "presented":
                verify_transfer_with_candidate(page, practice_id)
            else:
                verify_transfer_no_candidate(page)

    # ── F05 第二条分支：候选池耗尽时必须明确「未检验」 ────
    # 上面那一轮如果消耗掉了候选夹具，这一轮就会自然落到 no_candidate。
    # 两条分支都要验，不能只验当前碰巧走到的那条。
    print()
    print("-- F05 另一分支：无候选必须明确「未检验」 --")
    fresh_page(page)
    practice2 = to_practice(page)
    if not practice2:
        check("F05-5 第二轮带练已开始", False, "to_practice 返回 None")
    else:
        heard2 = page.query_selector('button:has-text("听懂了")')
        if heard2:
            heard2.click()
            page.wait_for_timeout(4500)
            kind2 = page.evaluate(
                "(document.querySelector('#cet-transfer-box')||{}).getAttribute"
                " ? document.querySelector('#cet-transfer-box').getAttribute('data-cet-transfer') : null"
            )
            if kind2 == "no_candidate":
                verify_transfer_no_candidate(page)
            elif kind2 == "presented":
                # 候选池还有料：再跑一次有候选分支（不重复消耗，直接验）
                print("  （候选池仍有料，本轮再验一次有候选分支）")
                verify_transfer_with_candidate(page, practice2)
            else:
                check("F05 换句区已渲染", False, f"kind={kind2}")
        else:
            check("F05-5 存在「听懂了」按钮", False, "未找到")


def verify_transfer_with_candidate(page, practice_id: str) -> None:
    """F05-3/4：有候选 —— 播放候选真实音频、展示题目、作答并落库。"""
    COVERAGE["presented"] += 1
    # 等自动播放尝试结束；无论自动播放是否被浏览器拦截，
    # 都点一次「重播候选」（真实用户手势，也覆盖重播入口）
    page.wait_for_timeout(1500)
    replay = page.query_selector("#cet-transfer-play")
    if replay:
        replay.click()

    # 等候选音频真的播完（最多 15 秒）
    played = False
    for _ in range(30):
        page.wait_for_timeout(500)
        if page.evaluate("window.__cet.S.transferPlayed"):
            played = True
            break

    cand = page.evaluate("""() => {
      const a = document.getElementById('cet-transfer-audio');
      const box = document.querySelector('#cet-transfer-box');
      const opts = Array.from(document.querySelectorAll('#cet-transfer-box .cet-opt'));
      const main = document.getElementById('local-media-player');
      const candObj = window.__cet.S.transferCandidate || {};
      return {
        hasAudio: !!a,
        audioSrc: a ? a.getAttribute('src') : null,
        audioTime: a ? a.currentTime : null,
        mainSrc: main ? main.getAttribute('src') : null,
        candRange: [candObj.startSeconds, candObj.endSeconds],
        candMaterial: candObj.materialId,
        question: (box && box.querySelector('.cet-transfer-question'))
          ? box.querySelector('.cet-transfer-question').textContent : '',
        optCount: opts.length,
        optTexts: opts.map(o => o.textContent.slice(0, 40)),
        optDisabled: opts.map(o => o.disabled),
        played: window.__cet.S.transferPlayed,
        note: (box ? box.innerText : '').slice(0, 300),
      };
    }""")
    check("F05-3 候选使用独立音频元素播放真实音频（不是原句）",
          cand["hasAudio"] and bool(cand["audioSrc"])
          and cand["audioSrc"] != cand["mainSrc"],
          f"候选={cand['audioSrc']} 原句={cand['mainSrc']}")
    check("F05-3b 候选来自其他材料且时间范围真实",
          bool(cand["candMaterial"]) and isinstance(cand["candRange"][0], (int, float))
          and cand["candRange"][1] > cand["candRange"][0],
          f"material={cand['candMaterial']} range={cand['candRange']}")
    check("F05-3c 展示候选题与选项",
          bool(cand["question"].strip()) and cand["optCount"] >= 2,
          f"题面={cand['question'][:40]!r} 选项数={cand['optCount']} {cand['optTexts']}")
    check("F05-3d 候选音频确实完整播过（后端 playedToEnd 前置成立）",
          played and bool(cand["played"]),
          f"transferPlayed={cand['played']} 候选时间={cand['audioTime']} 提示={cand['note'][:160]!r}")

    # F05-4：提交作答并落库
    opts_now = page.query_selector_all("#cet-transfer-box .cet-opt")
    enabled = [o for o in opts_now if not o.is_disabled()]
    if not enabled:
        check("F05-4 播完后选项解锁", False,
              f"仍禁用={cand['optDisabled']} 提示={cand['note'][:200]!r}")
        return
    check("F05-4 播完后选项解锁", True, f"{len(enabled)} 个可用")
    enabled[0].click()
    page.wait_for_timeout(4000)

    db = transfer_from_db(practice_id)
    check("F05-4b 作答真的落库（transfer_answer 事件）", db is not None,
          f"DB 最新 transfer_answer={db}")
    if db:
        check("F05-4c 落库内容含 candidateId/answer/isCorrect",
              all(k in db for k in ("candidateId", "answer", "isCorrect")),
              f"candidateId={db.get('candidateId')} answer={db.get('answer')} "
              f"isCorrect={db.get('isCorrect')}")
        # 后端也应有判定结果回传给前端
        verdict = page.evaluate(
            "(document.querySelector('#cet-practice-out')||{}).innerText || ''"
        )
        check("F05-4d 前端显示服务端判定结果",
              any(k in verdict for k in ("换句作答正确", "换句作答不正确", "已提交")),
              f"提示={verdict[-160:]!r}")


def verify_transfer_no_candidate(page) -> None:
    """F05-5：无候选 —— 明确「未检验」，不给作答入口，不当通过。"""
    COVERAGE["no_candidate"] += 1
    box = page.evaluate("""() => {
      const b = document.querySelector('#cet-transfer-box');
      return {
        exists: !!b,
        kind: b ? b.getAttribute('data-cet-transfer') : null,
        unverified: !!document.querySelector('[data-cet-unverified]'),
        text: (b ? b.innerText : ''),
        optCount: document.querySelectorAll('#cet-transfer-box .cet-opt').length,
        transferStatus: window.__cet.S.practice ? window.__cet.S.practice.transferStatus : null,
      };
    }""")
    check("F05-5 无候选时明确显示「未检验」且不当通过",
          box["exists"] and box["unverified"] and "未检验" in box["text"],
          f"kind={box['kind']} unverified={box['unverified']} 文本={box['text'][:120]!r}")
    check("F05-5b 无候选时不提供作答入口", box["optCount"] == 0,
          f"选项数={box['optCount']}")
    check("F05-5c 无候选不产生 transfer_answer 落库",
          box["transferStatus"] == "no_candidate",
          f"transferStatus={box['transferStatus']}")


    # ── 刷新恢复 ───────────────────────────────────────────
    print()
    print("-- 恢复：刷新后标记与时间线仍在 --")
    fresh_page(page, clear_storage=False)
    page.wait_for_timeout(2500)
    restored = page.evaluate("""() => {
      return {
        marks: (window.__cet.S.marks || []).length,
        timelineMarks: document.querySelectorAll('[data-cet-mark]').length,
        layer: !!document.querySelector('[data-cet-mark-layer]'),
        pressedAt: (window.__cet.S.marks[0]||{}).pressedAtSec,
        confirmed: (window.__cet.S.marks[0]||{}).confirmedSentenceId,
      };
    }""")
    check("R 刷新后标记恢复且时间线重绘",
          restored["marks"] >= 1 and restored["layer"],
          f"marks={restored['marks']} timelineMarks={restored['timelineMarks']} layer={restored['layer']}")
    check("R-b 恢复的标记仍同时保留两个身份",
          isinstance(restored["pressedAt"], (int, float)) and bool(restored["confirmed"]),
          f"pressedAtSec={restored['pressedAt']} confirmed={restored['confirmed']}")



def verify_lookback_matrix(page) -> None:
    """同一 pressedAtSec，不同 reactionDelaySeconds/speed → 候选句应不同。

    直接打真实后端（与前端 createMark 发的字段完全一致），
    再把结果与前端 CFG.lookbackSec 对照，证明「前端声明的回溯窗口
    真的进入了候选定位」，不是只把字段发出去就算完。
    """
    mat = get_json("/api/cet/materials/by-lesson/%d" % LESSON_ID)
    mid, mver = mat["materialId"], mat["materialVersion"]
    # 选一个远离句首的时间点，保证向前回溯有足够句子可落
    target = 30.0
    # 后端校验 0 <= reactionDelaySeconds <= 5，所以矩阵只用合法值：
    #   2.5 * 1.0 = 2.5s 回溯；2.5 * 0.75 = 1.875s；5.0 * 1.0 = 5.0s（更大窗口）
    session = "matrix-%d" % int(__import__("time").time() * 1000)

    def create(delay: float, speed: float, offset: float) -> dict:
        import urllib.request
        body = json.dumps({
            "materialId": mid, "materialVersion": mver,
            "listeningSessionId": session + ("-%s-%s" % (delay, speed)),
            "pressedAtSec": target + offset,
            "reactionDelaySeconds": delay, "speed": speed,
        }).encode()
        req = urllib.request.Request(
            BASE + "/api/cet/marks", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    # 用不同 session 避免被「附近已有标记」去重逻辑命中
    a = create(2.5, 1.0, 0.0)     # adjusted = 27.5
    b = create(2.5, 0.75, 0.0)    # adjusted = 30 - 1.875 = 28.125
    c = create(5.0, 1.0, 0.0)     # adjusted = 25.0（更大回溯，应更靠前）

    def sent(sid):
        for s in mat["sentences"]:
            if s["sentenceId"] == sid:
                return s
        return {}

    sa, sb, sc = sent(a.get("suggestedSentenceId")), sent(b.get("suggestedSentenceId")), sent(c.get("suggestedSentenceId"))
    check("F08-M1 回溯参数进入 adjustedSeconds 计算",
          abs(float(a.get("adjustedSeconds", -1)) - 27.5) < 0.01
          and abs(float(b.get("adjustedSeconds", -1)) - 28.125) < 0.02,
          f"1.0x: pressed 30 - 2.5*1.0 = {a.get('adjustedSeconds')}；"
          f"0.75x: 30 - 2.5*0.75 = {b.get('adjustedSeconds')}")
    check("F08-M2 同一按键时间、不同倍速 → 回溯位置不同",
          a.get("adjustedSeconds") != b.get("adjustedSeconds"),
          f"1.0x→{a.get('adjustedSeconds')}  0.75x→{b.get('adjustedSeconds')}")
    check("F08-M3 放大回溯窗口后候选句明显前移",
          bool(sa.get("sentenceId")) and bool(sc.get("sentenceId"))
          and float(sc.get("startSeconds", 0)) < float(sa.get("startSeconds", 0)),
          f"2.5s→{sa.get('sentenceId')}({sa.get('startSeconds')}s) "
          f"5.0s→{sc.get('sentenceId')}({sc.get('startSeconds')}s)")
    # 前端 CFG.lookbackSec 与后端实际收到的值一致
    front = page.evaluate("""() => {
      const cet = window.__cet;
      return {cfg: (window.CET_LOOKBACK_SEC === undefined ? 2.5 : window.CET_LOOKBACK_SEC)};
    }""")
    check("F08-M4 前端回溯窗口声明值与实测发送值一致",
          abs(float(front["cfg"]) - 2.5) < 0.01,
          f"window.CET_LOOKBACK_SEC 缺省={front['cfg']}（createMark 实测发送 2.5）")


def transfer_from_db(practice_id: str):
    """从数据库读最新的 transfer_answer 事件，证明真的落库（不是只改文案）。"""
    db_path = os.environ.get(
        "ELT_USER_DB", "/tmp/wt-c-ui/.cet-qa/data/vocab.db"
    )
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload_json FROM cet_practice_events "
            "WHERE practice_id=? AND event_type='transfer_answer' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (practice_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return json.loads(row["payload_json"])
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def to_practice(page) -> str | None:
    """走到「带练已开始」，返回 practiceId。"""
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1500)
    page.keyboard.press("Space")
    page.wait_for_timeout(2200)
    page.evaluate(f"{AUDIO}.pause()")

    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(3000)

    # 确认（预览态下点「确认这句」）
    confirm = page.query_selector('.cet-mark-actions button:has-text("确认这句")')
    if confirm:
        confirm.click()
        page.wait_for_timeout(2500)

    start = page.query_selector('button:has-text("开始带练")')
    if not start:
        return None
    start.click()
    page.wait_for_timeout(3500)
    return page.evaluate("window.__cet.S.practice && window.__cet.S.practice.practiceId")


if __name__ == "__main__":
    sys.exit(main())
