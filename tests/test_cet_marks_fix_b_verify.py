"""卡点模块 6 项缺陷 · 独立交叉验证（子代理 B）。

**本文件的目的不是「测试通过」，而是独立复核子代理 A 的修复是否真的成立。**
因此：
  - 不复用 A 的测试文件（`tests/test_cet_marks_fix_a_*.py`），断言全部自己写；
  - 不信 A 的交付说明，每个结论都从**真实服务 / 真实浏览器 / 真实库**取证；
  - 后端断言直接打 API，前端断言直接读渲染后的 DOM 与 `window.__cet.S`。

## 6 项缺陷与验证口径

| # | 缺陷 | 验证方式 |
| --- | --- | --- |
| 1 | 打完标记自动回听，抢走播放位置 | 浏览器：关闭自动回听后连按 3 次 M，`currentTime` 单调递增不回退 |
| 2 | 连续标记重复落同一句 | 浏览器：1.5s 内连按 2 次 M，`S.marks.length` == DOM 行数 |
| 3 | 无题/无时间轴材料出现假入口 | 浏览器：无时间轴材料工作区 `#cet-toolbar` 不存在 |
| 4 | 标记时间与候选句口径混乱 | 静态 + 浏览器：列表文案显式区分「标记于」与「候选句」 |
| 5 | 删除按钮无条件可点 | 静态 + **真实 API 链路**：已带练标记的删除按钮必须禁用 |
| 6 | 时间线刻度与列表口径不一致 | 浏览器：刻度位置 == 候选句起点，点刻度跳到候选句 |

## 运行

```bash
# 纯静态/后端（不需要服务）
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py

# 含浏览器（需要真实服务 + 材料）
CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py
```

⚠️ pytest **只设 `ELT_CONFIG_DIR`**，绝不设 `ELT_USER_DB`（会覆盖 monkeypatch 造成库污染）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")
EVIDENCE = REPO / "docs" / "cet-base" / "卡点修复" / "evidence"
LISTENING_JS = REPO / "frontend" / "static" / "cet-listening.js"
LISTENING_CSS = REPO / "frontend" / "static" / "cet-listening.css"


def _service_up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


needs_service = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达（示例：CET_BASE_URL=http://127.0.0.1:5282）",
)


# ══════════════════════════════════════════════════════════════════════
# 后端 API 小工具
# ══════════════════════════════════════════════════════════════════════
def api(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def api_get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def material_for_lesson(lesson_id: int) -> dict:
    return api_get(f"/api/cet/materials/by-lesson/{lesson_id}")


def first_material_with_timeline() -> dict:
    """取一份**有时间轴**的材料（sentences 非空）。"""
    data = api_get("/api/cet/materials")
    items = data.get("items") or data.get("materials") or []
    for item in items:
        lesson_url = str(item.get("lessonUrl") or "")
        m = re.search(r"/workspace/(\d+)", lesson_url)
        if not m:
            continue
        detail = material_for_lesson(int(m.group(1)))
        if detail.get("sentences"):
            return detail
    pytest.skip("隔离库里没有带时间轴的四六级材料（先跑 import_materials.py --all）")


def material_without_timeline() -> dict:
    """取一份**无时间轴**材料（sentences 为空）。没有就 skip。"""
    data = api_get("/api/cet/materials")
    items = data.get("items") or data.get("materials") or []
    for item in items:
        lesson_url = str(item.get("lessonUrl") or "")
        m = re.search(r"/workspace/(\d+)", lesson_url)
        if not m:
            continue
        detail = material_for_lesson(int(m.group(1)))
        if not detail.get("sentences"):
            return detail
    pytest.skip("隔离库里没有无时间轴材料（用只有音频的导入造一份）")


# ══════════════════════════════════════════════════════════════════════
# 缺陷 1 · 打完标记自动回听，抢走播放位置
# ══════════════════════════════════════════════════════════════════════
# 原始证据（主代理实测）：按 M 时 currentTime=2.96 → 按完 100ms 变成 0.03
# 根因：createMark() 无条件 previewMark(..., {autoplay:true}) → replayRange()
#       → md.currentTime = sent.startSeconds（seek 回候选句起点）
def test_defect1_no_unconditional_autoplay_in_source():
    """静态：createMark 里不能无条件调 previewMark(autoplay:true)。

    这是缺陷 1 的根因层断言 —— 即使浏览器测试因环境跳过，这一条也能拦住回退。
    """
    src = LISTENING_JS.read_text(encoding="utf-8")
    # 截取 createMark 函数体（到下一个顶层 function 声明）
    start = src.index("async function createMark(")
    body = src[start:start + 4000]

    # 断言口径修正（实测踩过）：不能要求 previewMark(...autoplay) 字符串完全消失 ——
    # 正确修法是「保留能力但默认关闭」，调用本身可以留着，但必须被开关包住。
    # 所以断言：该调用若存在，前面必须有 `if (S.autoplayOnMark)` 之类的守卫。
    call = "previewMark(mark, mark.suggestedSentenceId, { autoplay: true })"
    assert "autoplayOnMark" in body or "isAutoplayOnMark" in body, (
        "createMark 没有读取「标记后自动回听」开关（缺陷 1 未修）："
        "无条件的 previewMark(..., {autoplay:true}) 会把播放位置 seek 回候选句起点"
    )
    if call in body:
        idx = body.index(call)
        guard = body[max(0, idx - 320):idx]
        assert "autoplayOnMark" in guard, (
            "createMark 里的 previewMark(..., {autoplay:true}) 没有被「标记后自动回听」开关包住 —— "
            f"它仍然无条件执行（缺陷 1 未修）。调用点前 320 字符：{guard!r}"
        )


@needs_service
def test_defect1_marking_does_not_move_playhead():
    """后端口径：标记本身**不改播放位置**（位置是前端行为，这里锁数据契约）。

    `pressedAtSec` 必须等于按键时刻，不能被后端改写成候选句起点 ——
    否则前端即使不 seek，记录下来的位置也已经错位。
    """
    mat = first_material_with_timeline()
    sess = "d1-" + uuid.uuid4().hex[:8]
    pressed = 5.6
    status, mark = api(
        "POST",
        "/api/cet/marks",
        {
            "materialId": mat["materialId"],
            "materialVersion": mat["materialVersion"],
            "listeningSessionId": sess,
            "pressedAtSec": pressed,
            "reactionDelaySeconds": 2.5,
            "speed": 1.0,
        },
    )
    assert status == 201, mark
    assert abs(mark["pressedAtSec"] - pressed) < 0.001, (
        f"pressedAtSec 被后端改写：传入 {pressed}，返回 {mark['pressedAtSec']}"
    )
    # adjustedSeconds 是回溯后的吸附点，允许不同，但它不能顶替 pressedAtSec
    assert mark["adjustedSeconds"] <= mark["pressedAtSec"], (
        "adjustedSeconds 应小于等于 pressedAtSec（回溯是往前找）"
    )


# ══════════════════════════════════════════════════════════════════════
# 缺陷 2 · 连续标记重复落同一句
# ══════════════════════════════════════════════════════════════════════
# 原始证据：连按 3 次 M：5.60 → 2.14 → 2.14（后两次同点）；库里 v2-1-0 出现 2 次
# 根因：后端 1.5s 去重返回**已有**标记，前端无条件 S.marks.push → 同一 markId 入列两次
@needs_service
def test_defect2_backend_dedup_returns_same_mark_id():
    """后端：1.5s 内同位置重复按键必须返回**同一** markId，且标 deduplicated。"""
    mat = first_material_with_timeline()
    sess = "d2-" + uuid.uuid4().hex[:8]
    payload = {
        "materialId": mat["materialId"],
        "materialVersion": mat["materialVersion"],
        "listeningSessionId": sess,
        "pressedAtSec": 5.600,
        "reactionDelaySeconds": 2.5,
        "speed": 1.0,
    }
    st1, m1 = api("POST", "/api/cet/marks", payload)
    st2, m2 = api("POST", "/api/cet/marks", payload)

    assert st1 == 201 and st2 == 201
    assert m1["markId"] == m2["markId"], (
        f"1.5s 内同位置重复按键返回了不同 markId：{m1['markId']} vs {m2['markId']}"
    )
    # deduplicated 字段是主代理加固新增的；缺失说明后端加固没进这个分支
    assert m1.get("deduplicated") is False, f"第 1 次应 deduplicated=false，实际 {m1.get('deduplicated')}"
    assert m2.get("deduplicated") is True, f"第 2 次应 deduplicated=true，实际 {m2.get('deduplicated')}"


@needs_service
def test_defect2_backend_stores_only_one_row():
    """后端：连按 2 次后，库里该会话只有 1 条标记（不是 2 条）。"""
    mat = first_material_with_timeline()
    sess = "d2r-" + uuid.uuid4().hex[:8]
    payload = {
        "materialId": mat["materialId"],
        "materialVersion": mat["materialVersion"],
        "listeningSessionId": sess,
        "pressedAtSec": 7.2,
        "reactionDelaySeconds": 2.5,
        "speed": 1.0,
    }
    for _ in range(2):
        api("POST", "/api/cet/marks", payload)

    listing = api_get(
        f"/api/cet/marks?materialId={urllib.parse.quote(mat['materialId'])}"
        f"&listeningSessionId={urllib.parse.quote(sess)}"
    )
    items = listing.get("items") or listing.get("marks") or []
    assert len(items) == 1, f"连按 2 次后库里应有 1 条标记，实际 {len(items)} 条：{[i['markId'] for i in items]}"


def test_defect2_frontend_dedupes_by_mark_id_in_source():
    """静态：前端 createMark 必须按 markId 去重，不能无条件 push。

    根因层断言：后端去重后返回同一 markId，前端若无条件 push 就会
    「UI 显示 5 条 / 库里 2 条」。
    """
    src = LISTENING_JS.read_text(encoding="utf-8")
    start = src.index("async function createMark(")
    body = src[start:start + 4000]

    assert "S.marks.push(mark)" not in body or "findIndex" in body, (
        "createMark 无条件 S.marks.push(mark)，没有按 markId 去重（缺陷 2 根因未修）"
    )
    assert "findIndex" in body, "createMark 没有按 markId 查重"
    assert "mark.markId" in body, "去重逻辑没有使用 mark.markId"


# ══════════════════════════════════════════════════════════════════════
# 缺陷 3 · 无题/无时间轴材料出现假入口
# ══════════════════════════════════════════════════════════════════════
# 原始证据：lesson 3（sentenceCount=0）仍有「开始卡点练习/今日复习」工具栏；
#          点进去按 M 才报「该材料没有可用句子时间轴」
def test_defect3_source_guards_against_empty_sentences():
    """静态：boot() 在 sentences 为空时必须不 mount 工具栏。"""
    src = LISTENING_JS.read_text(encoding="utf-8")
    start = src.index("async function boot(")
    body = src[start:start + 3000]

    assert "S.sentences.length" in body, (
        "boot() 没有检查 S.sentences.length —— 无时间轴材料仍会挂出假入口（缺陷 3 根因未修）"
    )
    # 守卫必须在 mount() 之前 return
    guard_pos = body.find("S.sentences.length")
    mount_pos = body.find("mount();")
    assert guard_pos != -1 and mount_pos != -1
    assert guard_pos < mount_pos, "空句子守卫必须在 mount() 之前（否则工具栏已经挂上了）"
    assert "return" in body[guard_pos:mount_pos], "空句子守卫里没有 return，仍会继续 mount"


@needs_service
def test_defect3_backend_rejects_mark_without_timeline():
    """后端：无时间轴材料建标记必须 409 TIMELINE_UNAVAILABLE。"""
    mat = material_without_timeline()
    status, body = api(
        "POST",
        "/api/cet/marks",
        {
            "materialId": mat["materialId"],
            "materialVersion": mat["materialVersion"],
            "listeningSessionId": "d3-" + uuid.uuid4().hex[:8],
            "pressedAtSec": 5.0,
            "reactionDelaySeconds": 2.5,
            "speed": 1.0,
        },
    )
    assert status == 409, f"无时间轴材料建标记应 409，实际 {status}：{body}"
    detail = body.get("detail") or {}
    assert detail.get("code") == "TIMELINE_UNAVAILABLE", f"错误码应为 TIMELINE_UNAVAILABLE，实际 {detail.get('code')}"


@needs_service
def test_defect3_no_timeline_material_reports_zero_sentences():
    """前提核对：我们用来测缺陷 3 的材料确实是「无时间轴」。"""
    mat = material_without_timeline()
    assert mat.get("sentences") == [], "用于缺陷 3 的材料必须 sentences 为空"
    avail = mat.get("availability") or {}
    assert avail.get("hasTimeline") is False or avail.get("sentenceCount", 0) == 0


# ══════════════════════════════════════════════════════════════════════
# 缺陷 4 · 标记时间与候选句口径混乱
# ══════════════════════════════════════════════════════════════════════
# 原始证据：列表显示 `0:05.60 | 待确认 | ... | 预览 0:00.01–0:03.49`
#          前者是按键时刻，后者是回溯吸附的句子，并排像标错
def test_defect4_list_labels_pressed_time_explicitly():
    """静态：列表里的按键时刻必须显式标注（不能裸 `0:05.60` 与候选句并排）。"""
    src = LISTENING_JS.read_text(encoding="utf-8")
    # 找 renderMarks 里生成 cet-mark-time 的那行
    match = re.search(r"const when = el\('span', 'cet-mark-time', (.+?)\);", src)
    assert match, "找不到 renderMarks 里的 cet-mark-time 渲染"
    expr = match.group(1)

    assert "标记于" in expr or "按键" in expr, (
        f"cet-mark-time 仍是裸时间（{expr!r}），与候选句区间并排会造成口径混乱（缺陷 4 未修）"
    )


def test_defect4_candidate_sentence_is_labelled_as_candidate():
    """静态：候选句区间必须标明是「候选句」，不是含糊的「预览」。"""
    src = LISTENING_JS.read_text(encoding="utf-8")
    assert "候选句" in src, "列表没有出现「候选句」字样，用户无法分辨这是系统建议而非按键位置"
    # 旧的含糊写法「预览 X–Y」必须消失
    assert "'预览 ' + fmt(" not in src and "`预览 ${" not in src, (
        "仍在使用含糊的「预览 X–Y」标注候选句（缺陷 4 未修）"
    )


# ══════════════════════════════════════════════════════════════════════
# 缺陷 5 · 删除按钮无条件可点
# ══════════════════════════════════════════════════════════════════════
# 原始证据：已带练的标记删除按钮照样可点，点了才弹「该标记已开始带练，不能删除」
#
# ⚠️ 本组用例是本次交叉验证的重点：后端拒绝条件是「cet_practices 有没有行」，
#    与 practice.status 无关；而前端 markState 返回 'done' 的条件是 status === 'complete'。
#    两者不等价 —— 「已开始带练但未完成」（status='active'）是最常见状态，
#    在这个状态下前端按钮仍可点、后端仍拒绝。
@needs_service
def test_defect5_practice_status_is_active_not_complete():
    """取证：刚开始的带练 practice.status 是 'active'，不是 'complete'。

    这条用例的作用是把「前端判断依据」与「后端拒绝依据」的错位钉在报告里。
    """
    mat = first_material_with_timeline()
    sess = "d5-" + uuid.uuid4().hex[:8]
    mid, mver = mat["materialId"], mat["materialVersion"]

    # 带练前置：先交卷
    st, att = api("POST", "/api/cet/attempts", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess, "answers": [],
    })
    assert st == 201, att

    st, mark = api("POST", "/api/cet/marks", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess,
        "pressedAtSec": 5.6, "reactionDelaySeconds": 2.5, "speed": 1.0,
    })
    assert st == 201, mark

    st, conf = api("PATCH", f"/api/cet/marks/{mark['markId']}", {
        "confirmedSentenceId": mark["suggestedSentenceId"],
    })
    assert st == 200, conf

    st, practice = api("POST", "/api/cet/practices", {"markId": mark["markId"]})
    assert st == 201, practice
    assert practice["status"] == "active", (
        f"刚开始的带练 status 应为 'active'，实际 {practice['status']!r}"
    )


@needs_service
def test_defect5_backend_rejects_delete_when_practice_exists():
    """后端：已带练（即使 status='active'）的标记删除必须 409 MARK_IN_PRACTICE。"""
    mat = first_material_with_timeline()
    sess = "d5b-" + uuid.uuid4().hex[:8]
    mid, mver = mat["materialId"], mat["materialVersion"]

    api("POST", "/api/cet/attempts", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess, "answers": [],
    })
    st, mark = api("POST", "/api/cet/marks", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess,
        "pressedAtSec": 8.1, "reactionDelaySeconds": 2.5, "speed": 1.0,
    })
    assert st == 201, mark
    api("PATCH", f"/api/cet/marks/{mark['markId']}", {"confirmedSentenceId": mark["suggestedSentenceId"]})
    st, practice = api("POST", "/api/cet/practices", {"markId": mark["markId"]})
    assert st == 201, practice

    status, body = api("DELETE", f"/api/cet/marks/{mark['markId']}")
    assert status == 409, f"已带练标记删除应 409，实际 {status}：{body}"
    assert (body.get("detail") or {}).get("code") == "MARK_IN_PRACTICE"


@needs_service
def test_defect5_backend_exposes_inpractice_field():
    """后端：`_mark_payload` 必须返回 `inPractice`，且与 delete 的 409 拒绝条件一致。

    **这是缺陷 5 的权威口径**（主代理 2026-11-07 新增）：
      - 新建标记 → inPractice=false
      - 开带练后 → list_marks 返回 inPractice=true
      - 此时 DELETE 必然 409 MARK_IN_PRACTICE
    两者必须同步，否则前端无论怎么禁用都会与实际不符。
    """
    mat = first_material_with_timeline()
    sess = "d5ip-" + uuid.uuid4().hex[:8]
    mid, mver = mat["materialId"], mat["materialVersion"]

    api("POST", "/api/cet/attempts", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess, "answers": [],
    })
    st, mark = api("POST", "/api/cet/marks", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess,
        "pressedAtSec": 11.3, "reactionDelaySeconds": 2.5, "speed": 1.0,
    })
    assert st == 201, mark
    assert mark.get("inPractice") is False, (
        f"新建标记 inPractice 应为 False，实际 {mark.get('inPractice')!r}（字段缺失说明后端加固未生效）"
    )

    api("PATCH", f"/api/cet/marks/{mark['markId']}", {
        "confirmedSentenceId": mark["suggestedSentenceId"],
    })
    st, practice = api("POST", "/api/cet/practices", {"markId": mark["markId"]})
    assert st == 201, practice

    listing = api_get(
        f"/api/cet/marks?materialId={urllib.parse.quote(mid)}"
        f"&listeningSessionId={urllib.parse.quote(sess)}"
    )
    items = listing.get("items") or listing.get("marks") or []
    target = next((i for i in items if i["markId"] == mark["markId"]), None)
    assert target is not None, "list_marks 没返回该标记"
    assert target.get("inPractice") is True, (
        f"开带练后 list_marks 应返回 inPractice=true，实际 {target.get('inPractice')!r}"
    )

    st, body = api("DELETE", f"/api/cet/marks/{mark['markId']}")
    assert st == 409, f"inPractice=true 的标记删除应 409，实际 {st}：{body}"
    assert (body.get("detail") or {}).get("code") == "MARK_IN_PRACTICE", (
        "inPractice=true 与 delete 的拒绝条件不一致"
    )


def test_defect5_inpractice_null_falls_back_to_clickable():
    """边界：`inPractice` 为 null（旧路径/字段缺失）时，删除按钮应按**可点**处理。

    主代理约定：`inPractice: null` 表示「这个返回路径没注入该字段」（如旧版本服务、
    或某条 payload 路径漏了）。此时前端不能因为拿不到值就一律禁用 —— 那会让
    所有标记都删不掉。正确行为是「可点」，由后端 409 做最终兜底。

    静态断言：前端的判断必须写成「明确为 true 才禁用」，而不是「不是 false 就禁用」。
    """
    src = LISTENING_JS.read_text(encoding="utf-8")
    rm_start = src.index("function buildMarkMenu(")
    rm_body = src[rm_start:rm_start + 5000]

    # 必须是 === true 这类「显式真值」判断，不能是 !== false / !m.inPractice
    import re as _re
    explicit_true = _re.search(r"inPractice\s*===\s*true", rm_body)
    negated = _re.search(r"!\s*m\.inPractice|inPractice\s*!==\s*false", rm_body)

    assert explicit_true and not negated, (
        "删除菜单项把「inPractice 非 false」当成禁用条件 —— 当 inPractice 为 null（旧路径/字段缺失）时"
        "会把所有标记都锁死。应写成「inPractice === true 才禁用」，null 按可点处理（后端兜底）。"
        f"删除菜单代码块：{rm_body!r}"
    )


def test_defect5_frontend_disable_condition_uses_inpractice():
    """静态：前端禁用删除按钮必须读后端权威字段 `inPractice`（不是猜本地 practice 状态）。

    **判定标准（主代理 2026-11-07 修订）**：
    后端 `_mark_payload` 已新增 `inPractice`，其取值与 `delete_mark()` 的 409 拒绝条件
    **完全一致**（都是「cet_practices 里有没有行」）。所以前端必须读它，而不是用
    `markState()==='done'`（那只覆盖 `practice.status==='complete'`）。

    实测坑（本用例曾假阳性通过）：基线里 `$('#cet-transfer-play').disabled = true`
    在文件别处也存在，宽松断言会被它骗过 —— 所以这里只看 renderMarks 的删除按钮块。
    """
    src = LISTENING_JS.read_text(encoding="utf-8")

    rm_start = src.index("function buildMarkMenu(")
    rm_body = src[rm_start:rm_start + 5000]

    # 断言 1：菜单删除项本身必须有禁用逻辑
    assert "btn.disabled = true" in rm_body, (
        "删除菜单项没有任何禁用逻辑（缺陷 5 完全未修）—— "
        "已带练的标记删除按钮仍无条件可点，用户点了才看到「不能删除」"
    )

    # 断言 2：禁用依据必须是 inPractice（后端权威字段）
    assert "inPractice" in rm_body, (
        "删除菜单项的禁用条件没有读后端权威字段 `inPractice`。"
        "后端 delete_mark() 是「cet_practices 有行就拒绝」，与 practice.status 无关；"
        "若前端用 markState()==='done'（仅 status==='complete'）判断，"
        "则 status='active'（刚开始带练，最常见）时按钮仍可点、点了才报错 —— 缺陷 5 未真正修复。"
    )


# ══════════════════════════════════════════════════════════════════════
# 缺陷 6 · 时间线刻度与列表口径不一致
# ══════════════════════════════════════════════════════════════════════
# 原始证据：时间线刻度用 pressedAtSec（5.084），列表说的是候选句（0–3.49）；
#          点刻度跳到候选句而非刻度位置
def test_defect6_timeline_uses_candidate_sentence_start():
    """静态：时间线刻度的位置必须来自候选句起点，不是 pressedAtSec。"""
    src = LISTENING_JS.read_text(encoding="utf-8")
    start = src.index("function renderTimelineMarks(")
    body = src[start:start + 3000]

    # 断言口径修正（实测踩过）：正确修法允许 pressedAtSec 作为**候选句缺失时的兜底**，
    # 所以不能要求它完全消失。关键断言是：候选句存在时，位置必须来自 startSeconds。
    assert "startSeconds" in body, (
        "时间线刻度没有使用候选句的 startSeconds，与列表口径不一致（缺陷 6 未修）"
    )
    if "Number(m.pressedAtSec) || 0" in body:
        idx = body.index("Number(m.pressedAtSec) || 0")
        ctx = body[max(0, idx - 260):idx]
        # 必须写成「sent ? sent.startSeconds : pressedAtSec」这种兜底形式
        assert "sent ?" in ctx or ": (Number(m.pressedAtSec)" in ctx or "startSeconds :" in ctx, (
            "时间线刻度无条件用 m.pressedAtSec 定位，没有先取候选句起点（缺陷 6 未修）。"
            f"上下文：{ctx!r}"
        )
    # 刻度位置与点击跳转必须用同一个来源（同一个 sent 变量）
    assert "sent" in body, "时间线没有先解析候选句对象"


def test_defect6_timeline_click_jumps_to_same_place_as_tick():
    """静态：点刻度跳的位置 == 刻度画的位置（同一来源，不能两套口径）。"""
    src = LISTENING_JS.read_text(encoding="utf-8")
    start = src.index("function renderTimelineMarks(")
    body = src[start:start + 4000]

    # 新交互：点刻度先开聚合气泡，气泡里的回听必须与刻度同一候选句。
    assert "openTimelinePopover(btn, group.items)" in body, (
        "点刻度没有打开气泡/弹层"
    )
    assert "cet-timeline-popover-play" in src, "气泡里没有回听按钮"
    assert "playMark(item.mark, item.sentenceId)" in src, (
        "气泡回听没有按同一候选句播放（与刻度位置不一致）"
    )


# ══════════════════════════════════════════════════════════════════════
# 浏览器端到端（真实服务 + 真实按键 + 真实 DOM）
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome",
                                  args=["--autoplay-policy=no-user-gesture-required"])
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True,
                                  args=["--autoplay-policy=no-user-gesture-required"])
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._verify_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _open_workspace_with_timeline(page, lesson_id: int) -> None:
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    page.wait_for_selector("#local-media-player", timeout=30000)
    # 等卡点模块 boot 完成（它会异步取材料与句子）
    page.wait_for_selector("#cet-toolbar", timeout=30000)


def _enter_marking_mode(page) -> None:
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)


def _lesson_id_for(material: dict) -> int:
    m = re.search(r"/workspace/(\d+)", str(material.get("lessonUrl") or ""))
    assert m, f"材料没有 lessonUrl：{material}"
    return int(m.group(1))


@needs_service
def test_browser_defect1_playhead_monotonic_when_autoplay_off(page):
    """🔴 缺陷 1 核心：关闭自动回听后连按 3 次 M，currentTime 必须单调递增不回退。

    这是任务书点名要真实浏览器验证的 3 条之一。
    判定：3 次按键前后的 currentTime 序列不能出现「后一个明显小于前一个」。
    """
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    _open_workspace_with_timeline(page, lesson_id)

    # 确保自动回听关闭（默认应为关闭）
    page.evaluate("""() => {
        if (window.__cet && window.__cet.setAutoplayOnMark) {
            window.__cet.setAutoplayOnMark(false, {silent: true});
        }
    }""")
    _enter_marking_mode(page)

    # 播放到 ~3s，然后连按 3 次 M，每次间隔 > 300ms（前端防连发阈值）且 < 1.5s（后端去重窗口）
    page.evaluate("""() => {
        const m = document.getElementById('local-media-player');
        m.currentTime = 3.0;
        return m.currentTime;
    }""")
    page.wait_for_timeout(150)

    samples = []
    for i in range(3):
        before = page.evaluate("() => document.getElementById('local-media-player').currentTime")
        page.keyboard.press("KeyM")
        page.wait_for_timeout(420)   # > 300ms 防连发，< 1.5s 去重窗口
        after = page.evaluate("() => document.getElementById('local-media-player').currentTime")
        samples.append({"i": i, "before": before, "after": after})
        # 每次按键后往前推一点，模拟真实播放推进
        page.evaluate("() => { const m = document.getElementById('local-media-player'); m.currentTime += 1.2; }")
        page.wait_for_timeout(120)

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "defect1-playhead.png"))
    (EVIDENCE / "defect1-playhead.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 核心断言：按键之后的位置不能明显小于按键之前（允许 0.25s 的媒体元素抖动）
    for s in samples:
        assert s["after"] >= s["before"] - 0.25, (
            f"第 {s['i'] + 1} 次按 M 后播放位置从 {s['before']:.2f}s 回退到 {s['after']:.2f}s —— "
            f"仍在自动回听抢播放位置（缺陷 1 未修）。全部样本：{samples}"
        )
    # 整体趋势必须递增（不能原地打转）
    assert samples[-1]["after"] > samples[0]["before"], (
        f"连按 3 次 M 后位置没有前进（{samples[0]['before']:.2f} → {samples[-1]['after']:.2f}）："
        f"标记仍在把播放位置拽回同一处。全部样本：{samples}"
    )


@needs_service
def test_browser_defect2_no_duplicate_mark_ids_in_state(page):
    """🔴 缺陷 2 核心：1.5s 内连按 3 次 M，`S.marks` 里不能出现重复 markId。

    ⚠️ 断言口径修正（实测踩过的坑）：
    「S.marks.length == DOM 行数」是**无效断言** —— DOM 本来就是按 S.marks 渲染的，
    两者天然相等，基线代码也能通过。真实症状是：
        后端 1.5s 去重返回**同一** markId → 前端无条件 push → S.marks 有重复 ID
        实测：S.marks.length=3 / 库里 2 行 / mark-xxx 出现 2 次
    所以正确的断言是：**S.marks 里的 markId 必须唯一**，且长度不超过库里的行数。
    """
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    _open_workspace_with_timeline(page, lesson_id)
    _enter_marking_mode(page)

    page.evaluate("""() => {
        const m = document.getElementById('local-media-player');
        m.currentTime = 5.6;
    }""")
    page.wait_for_timeout(200)

    # 连按 3 次，间隔 400ms（> 300ms 前端防连发，< 1.5s 后端去重窗口）
    # 刻意**不**手动推进播放位置 —— 复现主代理实测的「连按 3 次全落同一处」
    for _ in range(3):
        page.keyboard.press("KeyM")
        page.wait_for_timeout(400)
    page.wait_for_timeout(1200)

    state = page.evaluate("""() => {
        const S = window.__cet && window.__cet.S;
        const rows = document.querySelectorAll('#cet-marks .cet-mark');
        return {
            marksLen: S ? S.marks.length : -1,
            markIds: S ? S.marks.map(m => m.markId) : [],
            pressed: S ? S.marks.map(m => m.pressedAtSec) : [],
            domRows: rows.length,
            sessionId: S ? S.sessionId : null,
            materialId: S ? S.materialId : null,
        };
    }""")

    assert state["marksLen"] >= 1, "连按 3 次 M 后一个标记都没有"

    # 取库里的真实行数作为对照
    listing = api_get(
        f"/api/cet/marks?materialId={urllib.parse.quote(state['materialId'])}"
        f"&listeningSessionId={urllib.parse.quote(state['sessionId'])}"
    )
    db_items = listing.get("items") or listing.get("marks") or []

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "defect2-dedup.png"))
    (EVIDENCE / "defect2-dedup.json").write_text(
        json.dumps({"frontend": state, "dbRowCount": len(db_items),
                    "dbIds": [i["markId"] for i in db_items]},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    # 核心断言：前端状态里不能有重复 markId
    assert len(state["markIds"]) == len(set(state["markIds"])), (
        f"S.marks 里有重复 markId（缺陷 2 未修）：{state['markIds']}；"
        f"按键时刻 {state['pressed']}；库里只有 {len(db_items)} 行"
    )
    # 前端标记数不能超过库里的真实行数
    assert state["marksLen"] <= len(db_items), (
        f"前端 S.marks.length={state['marksLen']} 超过库里真实行数 {len(db_items)} —— "
        f"UI 显示了库里不存在的标记（缺陷 2 未修）。"
        f"前端 IDs={state['markIds']}，库里 IDs={[i['markId'] for i in db_items]}"
    )


@needs_service
def test_browser_defect3_no_toolbar_without_timeline(page):
    """🔴 缺陷 3 核心：无时间轴材料的工作区不得出现 #cet-toolbar（假入口）。"""
    mat = material_without_timeline()
    lesson_id = _lesson_id_for(mat)

    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    # 等足够久，让卡点模块 boot 走完（它会先请求 materials/by-lesson）
    page.wait_for_timeout(4000)

    state = page.evaluate("""() => ({
        toolbar: !!document.getElementById('cet-toolbar'),
        toggle: !!document.getElementById('cet-toggle-mode'),
        reviewBtn: !!document.getElementById('cet-review-btn'),
        timeline: !!document.getElementById('cet-timeline'),
    })""")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "defect3-no-timeline.png"), full_page=False)

    assert not state["toolbar"], (
        f"无时间轴材料（sentenceCount=0）仍挂出了 #cet-toolbar 假入口（缺陷 3 未修）：{state}"
    )
    assert not state["toggle"], f"仍挂出「开始卡点练习」按钮（缺陷 3 未修）：{state}"
    assert not state["reviewBtn"], f"仍挂出「今日复习」按钮（缺陷 3 未修）：{state}"


@needs_service
def test_browser_defect3_workspace_still_renders(page):
    """反向保护：不挂工具栏 ≠ 页面坏掉。无时间轴材料的播放器与页面必须正常。"""
    mat = material_without_timeline()
    lesson_id = _lesson_id_for(mat)

    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    page.wait_for_timeout(2500)

    assert page.evaluate("() => !!document.getElementById('local-media-player')"), (
        "无时间轴材料的播放器没渲染出来（守卫误伤了正常页面）"
    )
    assert not page._verify_errors, f"页面有 JS 错误：{page._verify_errors}"  # type: ignore[attr-defined]


@needs_service
def test_browser_defect6_timeline_tick_position_matches_candidate_sentence(page):
    """🟡 缺陷 6：时间线刻度位置 == 候选句起点（与列表口径一致）。"""
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    _open_workspace_with_timeline(page, lesson_id)
    _enter_marking_mode(page)

    page.evaluate("""() => {
        const m = document.getElementById('local-media-player');
        m.currentTime = 5.6;
    }""")
    page.wait_for_timeout(150)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1200)

    result = page.evaluate("""() => {
        const tick = document.querySelector('#cet-timeline-track [data-cet-mark]');
        const S = window.__cet && window.__cet.S;
        if (!tick || !S || !S.marks.length) return null;
        const mark = S.marks[0];
        const sent = (S.sentences || []).find(s =>
            s.sentenceId === (mark.suggestedSentenceId || mark.confirmedSentenceId));
        return {
            tickSec: Number(tick.getAttribute('data-cet-mark-sec')),
            tickSentence: tick.getAttribute('data-cet-mark-sentence'),
            pressedAtSec: mark.pressedAtSec,
            candidateStart: sent ? sent.startSeconds : null,
            candidateId: sent ? sent.sentenceId : null,
        };
    }""")

    if result is None:
        pytest.skip("时间线标记没有渲染出来（可能材料时长未知）")

    assert result["candidateStart"] is not None, "找不到候选句，无法比对口径"
    assert abs(result["tickSec"] - result["candidateStart"]) < 0.05, (
        f"时间线刻度画在 {result['tickSec']}s，但候选句起点是 {result['candidateStart']}s"
        f"（按键时刻 {result['pressedAtSec']}s）—— 刻度与列表口径不一致（缺陷 6 未修）"
    )
    assert result["tickSentence"] == result["candidateId"], (
        f"刻度关联的句子 {result['tickSentence']} 与候选句 {result['candidateId']} 不一致"
    )


@needs_service
def test_browser_defect4_mark_row_shows_both_denominations_distinctly(page):
    """🟡 缺陷 4：列表行里「标记于 X」与「候选句 A–B」必须都能看到且文案区分。"""
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    _open_workspace_with_timeline(page, lesson_id)
    _enter_marking_mode(page)

    page.evaluate("""() => {
        const m = document.getElementById('local-media-player');
        m.currentTime = 6.2;
    }""")
    page.wait_for_timeout(150)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1200)

    row_text = page.evaluate("""() => {
        const row = document.querySelector('#cet-marks .cet-mark');
        return row ? row.innerText : '';
    }""")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "defect4-row.png"))

    assert row_text, "标记行没有渲染"
    assert "标记于" in row_text, f"标记行没有显式标注按键时刻（缺陷 4 未修）：{row_text!r}"
    assert "候选句" in row_text, f"标记行没有标明候选句（缺陷 4 未修）：{row_text!r}"


@needs_service
def test_browser_defect5_delete_button_state_for_practiced_mark(page):
    """🟡 缺陷 5（浏览器侧）：已带练标记的删除按钮应禁用。

    本用例**优先验证后端权威口径**：通过 API 造一个「已开始带练但未完成」的标记
    （practice.status='active'，这是最常见状态），再打开工作区看删除按钮是否禁用。
    若前端只按 markState()==='done' 判断，这里就会红。
    """
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    mid, mver = mat["materialId"], mat["materialVersion"]

    # 用页面自己的 session，保证标记能被前端 restore() 拉回来
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    page.wait_for_selector("#cet-toolbar", timeout=30000)
    sess = page.evaluate("() => window.__cet.S.sessionId")

    # 交卷 → 建标记 → 确认 → 开始带练（status 会是 active）
    api("POST", "/api/cet/attempts", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess, "answers": [],
    })
    st, mark = api("POST", "/api/cet/marks", {
        "materialId": mid, "materialVersion": mver, "listeningSessionId": sess,
        "pressedAtSec": 9.4, "reactionDelaySeconds": 2.5, "speed": 1.0,
    })
    assert st == 201, mark
    api("PATCH", f"/api/cet/marks/{mark['markId']}", {"confirmedSentenceId": mark["suggestedSentenceId"]})
    st, practice = api("POST", "/api/cet/practices", {"markId": mark["markId"]})
    assert st == 201, practice
    assert practice["status"] == "active", f"预期 active，实际 {practice['status']}"

    # 重新打开工作区，让前端 restore() 拉到这个已带练的标记
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    page.wait_for_selector("#cet-toolbar", timeout=30000)
    page.wait_for_timeout(2500)

    state = page.evaluate("""(markId) => {
        const S = window.__cet && window.__cet.S;
        const marks = S ? S.marks : [];
        const target = marks.find(m => m.markId === markId);
        const rows = [...document.querySelectorAll('#cet-marks .cet-mark')];
        // 找到含该标记的行的删除按钮（行内没有 markId 属性，按位置兜底）
        const idx = marks.findIndex(m => m.markId === markId);
        const row = idx >= 0 ? rows[idx] : null;
        const del = row ? row.querySelector('.cet-btn-danger') : null;
        return {
            restored: !!target,
            marksLen: marks.length,
            domRows: rows.length,
            inPractice: target ? target.inPractice : null,
            delDisabled: del ? del.disabled : null,
            delTitle: del ? del.title : null,
            delAriaDisabled: del ? del.getAttribute('aria-disabled') : null,
            delBlockedAttr: del ? del.getAttribute('data-cet-mark-delete-blocked') : null,
            practiceStatus: S && S.practice ? S.practice.status : null,
            practiceMarkId: S && S.practice ? S.practice.markId : null,
        };
    }""", mark["markId"])

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "defect5-delete-button.png"))

    assert state["restored"], f"已带练的标记没有被 restore() 拉回来（无法验证删除按钮）：{state}"

    # 判定标准：后端 inPractice=true 时按钮必须禁用（与 409 拒绝条件一致）
    assert state["inPractice"] is True, (
        f"后端应给这个已带练标记返回 inPractice=true，实际 {state.get('inPractice')!r}。"
        f"若为 None/False，说明 list_marks 的 inPractice 注入没生效。完整状态：{state}"
    )
    assert state["delDisabled"] is True, (
        f"已带练标记（inPractice=true）的删除按钮**仍可点** —— 缺陷 5 未真正修复。"
        f"后端 delete_mark() 会 409 拒绝，用户点下去才看到「该标记已开始带练，不能删除」。"
        f"实测状态：{state}"
    )
    assert "带练" in (state["delTitle"] or "") or "不能删除" in (state["delTitle"] or ""), (
        f"删除按钮禁用了但 title 没说清原因（用户不知道为什么点不了）：{state['delTitle']!r}"
    )


# ══════════════════════════════════════════════════════════════════════
# 补充发现（我读 A 的 diff 时注意到的、他自己没测的隐蔽 bug）
# ══════════════════════════════════════════════════════════════════════
@needs_service
def test_extra_autoplay_toggle_does_not_swallow_m_key(page):
    """补充：勾选「标记后自动回听」开关后，按 M 必须仍能产生标记。

    为什么加这条（A 没测）：
    A 在 `setAutoplayOnMark()` 里加了一个 `blur` 处理，注释写着
    「勾完开关后焦点会留在复选框上，而 isTypingTarget() 把 input 一律当成输入目标
    → 之后按 M 全被吞掉」。这是一个真实且隐蔽的 bug，A 修对了 ——
    但他自己的测试文件里没有覆盖这条，属于「修对了但没锁住」，将来重构会回退。

    验证：真实点复选框 → 不手动 blur → 直接按 M → 必须产生标记。
    """
    mat = first_material_with_timeline()
    lesson_id = _lesson_id_for(mat)
    _open_workspace_with_timeline(page, lesson_id)
    _enter_marking_mode(page)

    before = page.evaluate("() => (window.__cet.S.marks || []).length")

    # 真实点击复选框（会产生 focus）
    checkbox = page.query_selector("#cet-autoplay-mark")
    assert checkbox is not None, "工具栏没有「标记后自动回听」复选框"
    checkbox.click()
    page.wait_for_timeout(300)

    # 不手动 blur，直接按 M —— 这正是 A 修的边界
    page.evaluate("""() => {
        const m = document.getElementById('local-media-player');
        m.currentTime = 15.3;
    }""")
    page.wait_for_timeout(150)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1200)

    after = page.evaluate("() => (window.__cet.S.marks || []).length")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "extra-autoplay-toggle.png"))

    assert after > before, (
        f"勾选「标记后自动回听」开关后按 M 没有产生标记（{before} → {after}）—— "
        f"焦点留在复选框上，isTypingTarget() 把 input 当输入目标吞掉了按键。"
        f"A 已修这个问题（setAutoplayOnMark 里 blur），本用例锁住它不回退。"
    )
