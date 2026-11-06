#!/usr/bin/env python
"""四六级底座 · 逐项自验（T01–T15，可自动化部分）。

与 `scripts/cet/lead_trial.py` 的分工：
- `lead_trial.py`：Lead 的**用户视角完整链路**（打开应用 → 播放 → 收藏 → 恢复）。
- 本脚本：QA 的**验收清单逐项检查**，按任务书 §6 的 T01–T15 编号组织，
  每项输出 操作 / 预期 / 实际 / 证据，并把结论落到 JSON + Markdown。

设计原则（重要）：
1. **不硬编码 lesson id**：所有材料一律从 `/api/cet/materials` 反查。
2. **不硬编码工作区 URL**：base 由 `--base` 传入，默认按 `--port` 拼。
3. **端口安全**：启动前检测端口；若被非本项目进程占用，直接拒绝，不误连。
4. **数据隔离**：默认使用临时目录做 `ELT_USER_DB` / `ELT_CONFIG_DIR`，
   绝不读写仓库正式库 `resources/vocab.db`。
5. **可复现**：一条命令从空库跑到结论，验收方照抄即可。

用法：
    # 全自动（自起服务、自建空库、自导材料、跑完自停）
    .venv/bin/python scripts/cet/verify_base.py

    # 指定端口（默认 5184，避开主工作区 5173）
    .venv/bin/python scripts/cet/verify_base.py --port 5184

    # 只跑接口层，跳过浏览器（无 Playwright 环境时）
    .venv/bin/python scripts/cet/verify_base.py --no-browser

    # 复用已在跑的服务（验收方自己起的），只做检查
    .venv/bin/python scripts/cet/verify_base.py --base http://127.0.0.1:5184 --reuse

    # 只跑指定项
    .venv/bin/python scripts/cet/verify_base.py --only T01,T02,T15

退出码：0 全部通过｜1 有失败项｜2 环境/前置条件不满足
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
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_PORT = 5184  # 冻结约定 §6：验证与交付用 5184，避开主工作区 5173

RESULTS: list[dict] = []


# ── 结果记录 ──────────────────────────────────────────────
def record(tid: str, item: str, ok: bool, expected: str, actual: str,
           evidence: str = "") -> bool:
    RESULTS.append({
        "tid": tid, "item": item, "pass": bool(ok),
        "expected": expected, "actual": actual, "evidence": evidence,
    })
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {item}")
    if not ok:
        print(f"         期望: {expected}")
        print(f"         实际: {actual}")
    return bool(ok)


def skip(tid: str, item: str, reason: str) -> None:
    RESULTS.append({"tid": tid, "item": item, "pass": None,
                    "expected": "", "actual": f"SKIP: {reason}", "evidence": ""})
    print(f"  [SKIP] {item} — {reason}")


# ── HTTP ─────────────────────────────────────────────────
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
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def http_text(url: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


# ── 端口 ─────────────────────────────────────────────────
def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def port_owner(port: int) -> str:
    """端口占用者描述。用于「不误连其他服务」的判断。"""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10)
        lines = [ln for ln in out.stdout.splitlines()[1:] if ln.strip()]
        return lines[0].strip() if lines else ""
    except Exception:  # noqa: BLE001
        return ""


def is_our_base(url: str) -> bool:
    """判断某 URL 上跑的是不是本项目底座（而不是别的服务）。

    依据 `/health` 的返回形状：本项目返回含 `sqlite` / `ffmpeg` 的 JSON。
    """
    status, body = http_json(f"{url}/health", timeout=5)
    if status != 200 or not isinstance(body, dict):
        return False
    keys = set(body)
    return bool({"sqlite", "ffmpeg"} & keys) or "status" in keys


# ── 服务生命周期 ──────────────────────────────────────────
class Server:
    def __init__(self, port: int, data_dir: pathlib.Path, log: pathlib.Path):
        self.port = port
        self.data_dir = data_dir
        self.log = log
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict:
        env = {**os.environ}
        env["ELT_HOST"] = "127.0.0.1"
        env["ELT_PORT"] = str(self.port)
        env["ELT_USER_DB"] = str(self.data_dir / "vocab.db")
        env["ELT_CONFIG_DIR"] = str(self.data_dir / "config")
        # 自验要证明「无 key 也能用」，显式清掉所有 AI key
        for k in ("AI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
                  "DASHSCOPE_API_KEY", "AI_BASE_URL"):
            env.pop(k, None)
        return env

    def start(self, timeout: float = 60.0) -> tuple[bool, str]:
        (self.data_dir / "config").mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.log, "w")
        self.proc = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "backend/fastapi_server.py"],
            cwd=str(ROOT), env=self.env(), stdout=fh, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if http_json(f"{self.base}/health", timeout=2)[0] == 200:
                return True, f"就绪 {self.base}"
            if self.proc.poll() is not None:
                tail = self.log.read_text(errors="replace")[-1500:] if self.log.exists() else ""
                return False, f"进程退出 code={self.proc.returncode}\n{tail}"
            time.sleep(0.5)
        return False, f"{timeout}s 内未就绪，日志见 {self.log}"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def import_materials(data_dir: pathlib.Path) -> tuple[bool, str]:
    env = {**os.environ}
    env["ELT_USER_DB"] = str(data_dir / "vocab.db")
    env["ELT_CONFIG_DIR"] = str(data_dir / "config")
    p = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "scripts/cet/import_materials.py", "--all"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    return p.returncode == 0, (p.stdout + p.stderr).strip()


# ── T01：空库启动、无 key、无队友模块 ────────────────────
def t01(server: Server, out_dir: pathlib.Path) -> dict:
    """空库（未导入任何材料）+ 无 AI key，首页仍可用、可导入、无虚假加载。"""
    print("\n── T01 空库启动、无 key、无队友模块 ──")
    base = server.base

    status, health = http_json(f"{base}/health")
    record("T01", "健康检查可用", status == 200 and isinstance(health, dict),
           "200 + JSON", f"{status} keys={sorted(health)[:6] if isinstance(health, dict) else health}")

    if isinstance(health, dict):
        key_status = health.get("ai_key") or health.get("key_status") or health.get("keys")
        record("T01", "无 key 时健康检查不阻断", status == 200,
               "200（key 缺失不影响）", f"key 字段={key_status}")
        record("T01", "SQLite 检查通过", bool((health.get("sqlite") or {}).get("available", True)),
               "available=True", json.dumps(health.get("sqlite"), ensure_ascii=False)[:120])
        record("T01", "FFmpeg 检查通过", bool((health.get("ffmpeg") or {}).get("available", True)),
               "available=True", json.dumps(health.get("ffmpeg"), ensure_ascii=False)[:120])

    status, html = http_text(f"{base}/")
    ok_home = status == 200 and len(html) > 500
    record("T01", "空库首页可打开", ok_home, "200 + HTML >500 字符",
           f"{status} len={len(html)}")
    (out_dir / "t01-empty-home.html").write_text(html, encoding="utf-8")

    # 空库时材料列表应为空数组而不是报错
    status, mats = http_json(f"{base}/api/cet/materials")
    items = mats.get("items", []) if isinstance(mats, dict) else None
    record("T01", "空库材料列表为空且不报错", status == 200 and items == [],
           "200 + items=[]", f"{status} items={items}")

    # 无虚假加载态
    #
    # 注意：只检查**渲染文本**，不能直接搜整页 HTML ——
    # 页面内联 JS 里有 'AI 正在分析句式…' 这类字符串，但那是用户点击
    # 「句式分析」后才显示的按需状态，不是启动时的虚假加载。
    # 因此先剥掉 <script>/<style> 再检查。
    visible = _strip_code(html)
    fake_phrases = ["正在生成内容大纲", "AI 正在分析", "字幕生成中", "正在转写"]
    hits = [p for p in fake_phrases if p in visible]
    record("T01", "首页无虚假加载文案（渲染文本）", not hits, "无虚假加载文案",
           f"命中={hits}")

    # 空库应有导入入口
    has_import = any(k in html for k in ("导入", "import", "上传"))
    record("T01", "空库有导入入口", has_import, "含「导入/上传」", f"found={has_import}")

    # 不强制模型配置：页面不应要求先填 key
    forced = any(k in html for k in ("请先配置 API Key", "必须配置密钥", "请先设置API"))
    record("T01", "不强制模型配置", not forced, "无强制 key 提示", f"命中={forced}")

    status, mode = http_json(f"{base}/api/cet/mode")
    ok_mode = status == 200 and isinstance(mode, dict) and "features" in mode
    record("T01", "CET 模式配置可用", ok_mode, "200 + features",
           f"{status} keys={sorted(mode)[:5] if isinstance(mode, dict) else mode}")
    if ok_mode:
        retired = mode.get("retired_keys", [])
        record("T01", "收束项已登记", isinstance(retired, list) and len(retired) > 0,
               "retired_keys 非空", f"{len(retired)} 项: {retired[:5]}")
    return {"mode": mode if ok_mode else None}


# ── T02：分类、搜索、最近学习 ────────────────────────────
def t02(base: str, ctx: dict) -> dict:
    print("\n── T02 四级/六级分类、搜索、最近学习 ──")
    status, mats = http_json(f"{base}/api/cet/materials")
    items = mats.get("items", []) if isinstance(mats, dict) else []
    record("T02", "材料列表返回数组", status == 200 and isinstance(items, list),
           "200 + items[]", f"{status} count={len(items)}")

    levels = {m.get("level") for m in items}
    record("T02", "存在四级材料", "cet4" in levels, "含 level=cet4",
           f"levels={sorted(x for x in levels if x)}")
    record("T02", "存在六级材料", "cet6" in levels, "含 level=cet6",
           f"levels={sorted(x for x in levels if x)}")

    for lv in ("cet4", "cet6"):
        s, f = http_json(f"{base}/api/cet/materials?level={lv}")
        fitems = f.get("items", []) if isinstance(f, dict) else []
        all_match = all(m.get("level") == lv for m in fitems)
        record("T02", f"级别筛选 {lv} 结果纯净", s == 200 and all_match,
               f"全部 level={lv}", f"{s} count={len(fitems)} all_match={all_match}")

    # 待分类（unknown level）入口
    s, u = http_json(f"{base}/api/cet/materials?level=unspecified")
    record("T02", "待分类可查询", s == 200, "200（可空）",
           f"{s} count={len(u.get('items', [])) if isinstance(u, dict) else '?'}")

    # 搜索：按标题片段
    if items:
        title = str(items[0].get("title") or "")
        frag = title[:4] if len(title) >= 4 else title
        s, r = http_json(f"{base}/api/cet/materials?q={urllib.parse.quote(frag)}")
        hit = r.get("items", []) if isinstance(r, dict) else None
        record("T02", "标题搜索可用", s == 200 and isinstance(hit, list),
               f"200 + 数组（片段={frag}）",
               f"{s} count={len(hit) if hit is not None else 'N/A'}")

    # 最近学习：写入进度后应反映
    target = next((m for m in items if m.get("level") in ("cet4", "cet6")), None)
    if target:
        lesson_url = target.get("lessonUrl") or ""
        lid = int(str(lesson_url).rstrip("/").rsplit("/", 1)[-1]) if lesson_url else 0
        if lid:
            http_json(f"{base}/api/v2/lessons/{lid}/progress", "POST",
                      {"last_position_seconds": 7.5, "last_segment_index": 2})
            s, prog = http_json(f"{base}/api/v2/lessons/{lid}/progress")
            pos = float((prog or {}).get("last_position_seconds", -1)) if isinstance(prog, dict) else -1
            record("T02", "学习进度可写可读", abs(pos - 7.5) < 0.05,
                   "7.5", f"read={pos}")
        ctx["lesson_id"] = lid
        ctx["lesson_url"] = lesson_url
        ctx["target"] = target
    return ctx


# ── T07/T08：播放器与原文（浏览器） ──────────────────────
def t07_t08_browser(base: str, ctx: dict, out_dir: pathlib.Path) -> dict:
    print("\n── T07 播放/拖动/倍速/逐句/循环　T08 原文/CET 高亮/查词 ──")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        for tid in ("T07", "T08"):
            skip(tid, "浏览器层", "Playwright 未安装")
        return ctx

    lesson_url = ctx.get("lesson_url")
    if not lesson_url:
        for tid in ("T07", "T08"):
            skip(tid, "浏览器层", "无可打开材料")
        return ctx

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        page.goto(f"{base}{lesson_url}", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        media = page.evaluate("""() => {
            const m = document.querySelector('#local-media-player')
                   || document.querySelector('audio')
                   || document.querySelector('video');
            if (!m) return null;
            return {src: (m.currentSrc||m.src||''), duration: m.duration,
                    paused: m.paused, readyState: m.readyState};
        }""")
        record("T07", "播放器已挂载", media is not None, "存在 media 元素",
               json.dumps(media, ensure_ascii=False)[:150] if media else "未找到")

        if media:
            probe = page.evaluate("""() => {
                const m = document.querySelector('#local-media-player')
                       || document.querySelector('audio') || document.querySelector('video');
                const out = {};
                try { m.currentTime = 5.0; out.seek = m.currentTime; } catch(e) { out.seekErr = String(e); }
                try { m.playbackRate = 1.5; out.rate = m.playbackRate; } catch(e) { out.rateErr = String(e); }
                try { m.playbackRate = 1.0; } catch(e) {}
                out.paused = m.paused; out.duration = m.duration;
                return out;
            }""")
            record("T07", "拖动定位真实生效",
                   isinstance(probe.get("seek"), (int, float)) and abs(probe["seek"] - 5.0) < 1.0,
                   "currentTime≈5.0", json.dumps(probe, ensure_ascii=False)[:160])
            record("T07", "倍速真实生效", probe.get("rate") == 1.5,
                   "playbackRate=1.5", str(probe.get("rate")))
            record("T07", "音频时长有效",
                   isinstance(probe.get("duration"), (int, float)) and probe["duration"] > 10,
                   "duration > 10s", str(probe.get("duration")))
            record("T07", "音源非空", bool(media.get("src")), "非空音频地址",
                   str(media.get("src"))[-70:])

        # 原文渲染
        transcript = page.evaluate("""() => {
            const sels = ['#subtitle-timeline','#subtitle-list','.subtitle-line',
                          '#transcript','.sentence-row','[data-sentence-index]'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.innerText.trim().length > 20)
                    return {sel: s, len: el.innerText.trim().length};
            }
            return {sel: null, len: 0};
        }""")
        record("T08", "原文已渲染", transcript["len"] > 20, "原文文本 >20 字符",
               json.dumps(transcript, ensure_ascii=False))

        # CET 高亮
        #
        # 注意：高亮只出现在**当前播放句**上，不同时间点的句子有没有 CET 词
        # 是数据决定的。所以不能「页面一打开就数高亮元素」——
        # 那样结果取决于当前播放位置，是顺序依赖的假失败。
        # 正确做法：先从字幕接口找一条**确实带高亮词**的句子，再 seek 过去验证渲染。
        hl_target = _find_highlighted_segment(base, ctx.get("lesson_id"))
        if hl_target:
            mid = (float(hl_target["start"]) + float(hl_target["end"])) / 2
            page.evaluate(
                """(t) => {
                    const m = document.querySelector('#local-media-player')
                           || document.querySelector('audio')
                           || document.querySelector('video');
                    if (m) m.currentTime = t;
                }""", mid)
            page.wait_for_timeout(1500)
            hl = page.evaluate("""() => {
                const els = document.querySelectorAll(
                  '.cet-word, .highlight-word, .cet4-word, .cet6-word, [data-cet-level]');
                return {count: els.length,
                        sample: Array.from(els).slice(0,5).map(e=>e.textContent.trim())};
            }""")
            record("T08", "CET 词表高亮渲染",
                   hl["count"] > 0,
                   f"带高亮词的句子（{hl_target.get('highlighted_words')}）应渲染出高亮",
                   json.dumps(hl, ensure_ascii=False)[:160])
        else:
            # 材料本身没有 CET 高亮词 → 这是数据事实，不是缺陷，明确记录
            record("T08", "CET 词表高亮数据存在", False,
                   "字幕接口应有 highlighted_words",
                   "材料字幕无任何 highlighted_words（可能词表未加载）")

        page.screenshot(path=str(out_dir / "t07-workspace.png"), full_page=False)
        record("T07", "工作区截图已存", (out_dir / "t07-workspace.png").exists(),
               "截图文件存在", str(out_dir / "t07-workspace.png"))

        # 区分三类：404（可选资源缺失，容忍）、409（真实缺陷，Lead 已报 B15）、
        # 其它（pageerror / 5xx，一律算失败）
        bad_404 = [e for e in errors if "404" in e or "favicon" in e]
        conflict = [e for e in errors if "409" in e]
        fatal = [e for e in errors if e not in bad_404 and e not in conflict]
        record("T07", "无致命 JS 运行时错误", not fatal, "无 pageerror / 5xx",
               json.dumps(fatal[:3], ensure_ascii=False))
        record("T07", "无 409 接口冲突", not conflict,
               "工作区不应出现 409",
               f"{len(conflict)} 条 409（已知缺陷 B15：/api/v2/lessons/{{id}}/reading）"
               if conflict else "无")
        browser.close()
    return ctx


# ── T12：入口收束 ────────────────────────────────────────
def t12_browser(base: str, out_dir: pathlib.Path) -> None:
    print("\n── T12 主页/工作区/收藏/设置及旧链接 ──")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip("T12", "浏览器层", "Playwright 未安装")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        page.goto(f"{base}/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        title = page.title()
        record("T12", "首页标题为 CET 口径", ("四六级" in title or "听力" in title),
               "含「四六级」或「听力」", title)

        # 泛化入口是否真的退出：必须区分「DOM 里有但隐藏」和「用户真的看得见」。
        # 任务书 §3.3 允许保留源码，只要求 CET 模式内不可见、不可聚焦。
        retired_hits = page.evaluate("""(kws) => {
            const hits = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let n;
            while ((n = walker.nextNode())) {
                const el = n.parentElement;
                if (!el || el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
                const txt = n.textContent;
                for (const kw of kws) {
                    if (!txt.includes(kw)) continue;
                    const st = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    const visible = st.display !== 'none' && st.visibility !== 'hidden'
                                    && st.opacity !== '0' && r.width > 0 && r.height > 0;
                    if (visible) hits.push({kw, tag: el.tagName,
                                            cls: (el.className || '').toString().slice(0, 40)});
                }
            }
            return hits.slice(0, 12);
        }""", ["YouTube", "Bilibili", "B站", "百度网盘", "记忆故事", "雅思复用", "情境造句"])
        record("T12", "泛化入口已退出（可见性判定）", not retired_hits,
               "无可见的泛化入口文案",
               f"可见命中={json.dumps(retired_hits, ensure_ascii=False)[:200]}")

        # 导航收束（Lead 已报 B2：导航含「个人档案」）
        nav_items = page.evaluate("""() => [...document.querySelectorAll(
            '.tab-btn, .cet-nav-item, nav a')].map(e => e.textContent.trim()).filter(Boolean)""")
        nav_retired = [t for t in nav_items
                       if any(k in t for k in ("个人档案", "课程", "词汇", "句子库"))]
        record("T12", "导航已收束为听力/收藏/设置", not nav_retired,
               "无「个人档案/课程/词汇/句子库」",
               f"导航={nav_items[:8]} 残留={nav_retired}")
        page.screenshot(path=str(out_dir / "t12-home.png"))

        for name, path in (("收藏", "/#favorites"), ("设置", "/#resources")):
            page.goto(f"{base}{path}", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            txt = page.inner_text("body")
            record("T12", f"{name}页可打开且有内容", len(txt.strip()) > 30,
                   ">30 字符", f"{len(txt.strip())} 字符")
        browser.close()


# ── T14：窄窗口 ──────────────────────────────────────────
def t14_browser(base: str, ctx: dict, out_dir: pathlib.Path) -> None:
    print("\n── T14 常用桌面宽度和窄窗口 ──")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        skip("T14", "浏览器层", "Playwright 未安装")
        return

    lesson_url = ctx.get("lesson_url") or "/"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        for w, h, label in ((1440, 900, "桌面"), (1280, 800, "桌面窄"),
                            (768, 1024, "平板"), (375, 780, "窄窗口")):
            page.set_viewport_size({"width": w, "height": h})
            page.goto(f"{base}{lesson_url}", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            ov = page.evaluate("""() => ({
                scrollW: document.documentElement.scrollWidth,
                clientW: document.documentElement.clientWidth,
                canScroll: document.documentElement.scrollHeight > window.innerHeight
            })""")
            ok = ov["scrollW"] <= ov["clientW"] + 4
            record("T14", f"{label} {w}x{h} 无横向溢出", ok,
                   "scrollWidth <= clientWidth+4", json.dumps(ov))
            if w == 375:
                page.screenshot(path=str(out_dir / "t14-narrow.png"))
        browser.close()


# ── 主流程 ───────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--base", default=None, help="复用已有服务（配合 --reuse）")
    ap.add_argument("--reuse", action="store_true", help="不启动服务，只做检查")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔，如 T01,T02")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-data", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    def want(tid: str) -> bool:
        return not only or tid in only

    out_dir = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="cet-verify-evidence-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  四六级底座 · 逐项自验（T01–T15 可自动化部分）")
    print(f"  仓库：{ROOT}")
    print(f"  证据目录：{out_dir}")
    print("=" * 70)

    # ── 前置：端口安全 ──
    server: Server | None = None
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="cet-verify-data-"))
    if args.reuse:
        base = (args.base or f"http://127.0.0.1:{args.port}").rstrip("/")
        if not is_our_base(base):
            print(f"\nX  {base} 上没有本项目底座在响应（不是 /health 形状）。")
            print("   为避免误连其他服务，已停止。请确认端口或用 --port 换一个。")
            return 2
        print(f"\n  复用已有服务：{base}")
    else:
        if port_listening(args.port):
            owner = port_owner(args.port)
            print(f"\nX  端口 {args.port} 已被占用：")
            print(f"   {owner}")
            if is_our_base(f"http://127.0.0.1:{args.port}"):
                print("   该端口上是本项目底座。若想复用它，加 --reuse；"
                      "否则用 --port 换一个空闲端口。")
            else:
                print("   占用者不是本项目底座（/health 形状不符），拒绝误连。")
                print("   请用 --port 换一个空闲端口。")
            return 2
        server = Server(args.port, data_dir, data_dir / "server.log")
        ok, msg = server.start()
        print(f"  启动服务：{msg}")
        if not ok:
            print("X  服务启动失败")
            return 2
        base = server.base

    ctx: dict = {}
    try:
        # T01 必须在导入材料前跑（空库语义）
        if want("T01"):
            ctx.update(t01(server or Server(args.port, data_dir, data_dir / "server.log"), out_dir))

        # 导入材料（幂等）
        print("\n── 导入样例材料（隔离库）──")
        ok, msg = import_materials(data_dir)
        record("T15", "样例材料导入成功", ok, "退出码 0", msg.splitlines()[-1][:160] if msg else "")

        if want("T02"):
            t02(base, ctx)

        if not args.no_browser:
            if want("T07") or want("T08"):
                t07_t08_browser(base, ctx, out_dir)
            if want("T12"):
                t12_browser(base, out_dir)
            if want("T14"):
                t14_browser(base, ctx, out_dir)
    finally:
        if server:
            server.stop()
        if not args.keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)

    # ── 汇总 ──
    passed = sum(1 for r in RESULTS if r["pass"] is True)
    failed = [r for r in RESULTS if r["pass"] is False]
    skipped = sum(1 for r in RESULTS if r["pass"] is None)

    report = {
        "base": base, "repo": str(ROOT), "head": _git_head(),
        "passed": passed, "failed": len(failed), "skipped": skipped,
        "total": len(RESULTS), "items": RESULTS,
    }
    (out_dir / "verify-base.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"  结果：{passed} passed / {len(failed)} failed / {skipped} skipped"
          f"（共 {len(RESULTS)} 项）")
    print(f"  HEAD：{report['head']}")
    print(f"  证据：{out_dir}/verify-base.json")
    print("=" * 70)
    if failed:
        print("\n失败项：")
        for r in failed:
            print(f"  [{r['tid']}] {r['item']}")
            print(f"        期望: {r['expected']}")
            print(f"        实际: {r['actual']}")
        return 1
    return 0


def _find_highlighted_segment(base: str, lesson_id: int | None) -> dict | None:
    """从字幕接口找一条带 CET 高亮词的句子（供 T08 定点验证高亮渲染）。"""
    if not lesson_id:
        return None
    status, data = http_json(f"{base}/api/v2/lessons/{lesson_id}/subtitles")
    if status != 200 or not isinstance(data, dict):
        return None
    for seg in data.get("segments") or []:
        if seg.get("highlighted_words"):
            return seg
    return None


def _strip_code(html: str) -> str:
    """剥掉 <script>/<style> 内容，只留渲染文本。

    T01 判「虚假加载文案」必须基于用户能看到的文字。
    直接搜整页 HTML 会把内联 JS 里的按需状态字符串误判成虚假加载。
    """
    out = []
    depth = 0
    tag = ""
    i = 0
    low = html.lower()
    while i < len(html):
        if depth == 0:
            nxt_s = low.find("<script", i)
            nxt_t = low.find("<style", i)
            cands = [x for x in (nxt_s, nxt_t) if x != -1]
            if not cands:
                out.append(html[i:])
                break
            nxt = min(cands)
            out.append(html[i:nxt])
            tag = "script" if nxt == nxt_s else "style"
            depth = 1
            i = nxt + len(tag) + 1
        else:
            end = low.find(f"</{tag}", i)
            if end == -1:
                break
            i = end + len(tag) + 2
            gt = html.find(">", i)
            i = (gt + 1) if gt != -1 else len(html)
            depth = 0
    return "".join(out)


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    sys.exit(main())
