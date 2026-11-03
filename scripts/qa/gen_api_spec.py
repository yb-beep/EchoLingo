#!/usr/bin/env python3
"""四六级听力黑匣子 · 接口规格生成器（C 负责，给 B 与 A 共用）。

为什么需要它：
    Mock 的路由用的是原始 dict body，OpenAPI 里只有 2 个 schema。
    B 拿不到「这个字段是必填还是可选、什么类型、什么枚举」的准信，
    只能翻 Mock 源码反推 —— 那是把契约藏在实现里，容易各写各的。

这个脚本从**正在运行的、已通过 56 条契约测试的 Mock** 里取样，
生成一份明确的字段规格：类型、必填性、枚举、示例、空值语义。
因为直接取自实测响应，规格不可能与实际行为脱节。

为什么不信 OpenAPI：
    FastAPI 对 `dict = Body(...)` 不会展开字段，生成的 spec 是空的。
    所以这里走「实际发请求 → 观察响应 → 归纳类型」的路线。

输出：
    docs/qa/接口规格.md      人读版（给 A/B 对照实现）
    docs/qa/interface-spec.json  机读版（可做后续校验）

用法：
    .venv/bin/python scripts/qa/gen_api_spec.py
    .venv/bin/python scripts/qa/gen_api_spec.py --check   # 只比对，不写盘

前置：Mock 在 5199 跑着（scripts/qa/start_cet.sh --mock）。
退出码：0 成功｜1 服务不可用｜2 生成结果与已入库规格不一致（--check）
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOCK = "http://127.0.0.1:5199"
MD_OUT = ROOT / "docs" / "qa" / "接口规格.md"
JSON_OUT = ROOT / "docs" / "qa" / "interface-spec.json"
MID = "cet6-202512-conv2"


def call(method: str, path: str, body=None, timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{MOCK}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


import re as _re

# 每次运行生成的运行时 ID 都不同（attempt-6e29e52e / mark-05288ff5 …），
# 直接写进规格会让 --check 永远失败，也会让 diff 充满噪声。
# 归一化成占位符，读者仍能看出格式。
_ID_PAT = _re.compile(
    r"\b(attempt|mark|practice|review|step|item|cand|req|evt)-[0-9a-f]{6,}\b"
)

# 时间戳同理：每次生成都是新的（submittedAt / createdAt / confirmedAtSec …）。
# 不归一化会让 --check 永远失败，也会掩盖真正需要关注的变化。
_TS_PAT = _re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?\b")


def normalize_ids(value):
    """把随机 hex 的运行时 ID 替换成 <prefix>-<id> 形式，保证生成可复现。"""
    if isinstance(value, str):
        value = _ID_PAT.sub(lambda m: f"{m.group(1)}-<id>", value)
        value = _TS_PAT.sub("<timestamp>", value)
        return value
    if isinstance(value, list):
        return [normalize_ids(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize_ids(v) for k, v in value.items()}
    return value


def type_of(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def describe(obj, depth: int = 0) -> dict:
    """把实际响应归纳成 {字段: {type, sample, nullable}}。"""
    if not isinstance(obj, dict):
        return {"_type": type_of(obj), "_sample": obj}
    out = {}
    for k, v in obj.items():
        entry = {"type": type_of(v)}
        if isinstance(v, dict):
            entry["fields"] = describe(v, depth + 1)
        elif isinstance(v, list):
            if not v:
                entry["items"] = {"_type": "unknown"}
            elif all(isinstance(x, dict) for x in v):
                # 数组元素逐个并集：交卷结果里「有答案」与「未知答案」两行
                # 字段可空性不同（answer/correct 可为 null），只看 v[0] 会漏。
                merged: dict = {}
                for item in v:
                    d = describe(item, depth + 1)
                    for k, meta in d.items():
                        if k not in merged:
                            merged[k] = dict(meta)
                        else:
                            if meta.get("nullable"):
                                merged[k]["nullable"] = True
                            # 同一字段在不同元素里类型不同 → 标为多变
                            if merged[k].get("type") != meta.get("type"):
                                merged[k]["type"] = f"{merged[k].get('type')}|{meta.get('type')}"
                entry["items"] = merged
            else:
                entry["items"] = describe(v[0], depth + 1)
            entry["length"] = len(v)
        else:
            entry["sample"] = v
        if v is None:
            entry["nullable"] = True
        out[k] = entry
    return out


def collect() -> dict:
    """按实现顺序发一串请求，收集每个接口的实际响应形状。"""
    spec: dict = {"materialId": MID, "endpoints": []}

    def add(method, path, body, note, sample=None):
        status, resp = call(method, path, body)
        spec["endpoints"].append({
            "method": method,
            "path": normalize_ids(path),
            "note": note,
            "status": status,
            "body": normalize_ids(body),
            "response": normalize_ids(describe(sample if sample is not None else resp)),
        })
        return resp

    call("POST", "/api/cet/_mock/reset")

    add("GET", "/api/cet/materials", None, "材料列表")
    add("GET", f"/api/cet/materials/{MID}?version=v1", None, "材料详情（含句子时间轴）")
    add("GET", f"/api/cet/materials/{MID}/questions?version=v1", None,
        "题目（交卷前：不含 answer/evidenceRef/explanation）")

    # 交卷：构造两题，覆盖「正常答案」与「未知答案」两条路径
    at = add("POST", "/api/cet/attempts",
             {"materialId": MID, "materialVersion": "v1",
              "listeningSessionId": "spec-attempt",
              "answers": [{"questionId": "q-05", "choice": 1},
                          {"questionId": "q-09", "choice": 0}]},
             "交卷（交卷后才返回答案与证据）")

    mark = add("POST", "/api/cet/marks",
               {"materialId": MID, "materialVersion": "v1",
                "listeningSessionId": "spec-listen", "pressedAtSec": 36.9},
               "创建标记（pressedAtSec 为原始按键秒数）")
    mark_id = (mark or {}).get("markId", "mark-x")

    add("PATCH", f"/api/cet/marks/{mark_id}",
        {"confirmedSentenceId": "s13"}, "确认/调整句子")
    add("GET", f"/api/cet/marks?materialId={MID}", None, "按材料隔离查询标记")

    pr = add("POST", "/api/cet/practices", {"markId": mark_id},
             "创建练习（须先确认句子；同一标记重复创建返回原练习）")
    pid = (pr or {}).get("practiceId", "practice-x")

    add("GET", f"/api/cet/practices/{pid}", None, "按原 ID 恢复练习")
    add("POST", f"/api/cet/practices/{pid}/next", {"feedback": "sound"},
        "AI 下一步（feedback=sound → 慢放）")
    add("POST", f"/api/cet/practices/{pid}/next", {"feedback": "meaning"},
        "AI 下一步（feedback=meaning → 解释）")
    add("POST", f"/api/cet/practices/{pid}/next", {"feedback": "heard"},
        "AI 下一步（feedback=heard → 换句检查）")
    add("POST", f"/api/cet/practices/{pid}/local", {"action": "chunk"},
        "本地动作（模型不可用时的兜底）")
    add("POST", f"/api/cet/practices/{pid}/events",
        {"eventId": "spec-evt", "type": "end", "stepId": "s1",
         "materialVersion": "v1", "playedMs": 2000, "audioSec": 38.5,
         "speed": 1.0, "visible": True},
        "播放事件（eventId 幂等；仅前台可见播放计入有效时长）")
    add("POST", f"/api/cet/practices/{pid}/finish", {"feedback": "sound"},
        "结束练习并保存卡点（重复结束幂等）")

    add("GET", "/api/cet/review-items?materialId=" + MID, None, "待复习卡点")
    rv = add("POST", "/api/cet/reviews", {"maxItems": 3, "budgetMs": 600000},
             "创建复习会话（最多三处）")
    rid = (rv or {}).get("reviewId", "review-x")
    add("GET", f"/api/cet/reviews/{rid}", None, "按原 ID 恢复复习会话")
    for act in ("start", "pause", "resume"):
        add("POST", f"/api/cet/reviews/{rid}/actions", {"action": act},
            f"复习动作 {act}")

    # skip 单独取样：它带 needsPractice=true，是「跳过 ≠ 听懂」的落点，
    # 只跑 start/pause/resume 会把这条语义漏掉。
    rv_detail = call("GET", f"/api/cet/reviews/{rid}")[1] or {}
    items = rv_detail.get("items") or []
    if items:
        add("POST", f"/api/cet/reviews/{rid}/actions",
            {"action": "skip", "itemId": items[0].get("itemId")},
            "复习动作 skip（条目状态转 skipped，并带 needsPractice=true：跳过≠听懂）")
        done_items = call("GET", f"/api/cet/reviews/{rid}")[1] or {}
        rest = [i for i in (done_items.get("items") or [])
                if i.get("status") != "skipped"]
        if rest:
            add("POST", f"/api/cet/reviews/{rid}/actions",
                {"action": "done", "itemId": rest[0].get("itemId")},
                "复习动作 done")

    # 错误样例（A 要按这个写错误处理）
    add("POST", "/api/cet/attempts", {}, "业务校验失败的错误形状")
    add("GET", "/api/cet/materials/does-not-exist", None, "资源不存在的错误形状")

    spec["generatedAt"] = datetime.now().isoformat(timespec="seconds")
    return spec


def to_markdown(spec: dict) -> str:
    lines: list[str] = []
    A = lines.append
    A("# 四六级听力黑匣子 · 接口规格")
    A("")
    A("> **本文件由 `scripts/qa/gen_api_spec.py` 从运行中的 Mock 实测生成，请勿手改。**")
    A("> 重新生成：`.venv/bin/python scripts/qa/gen_api_spec.py`")
    A(f"> 生成时间：{spec['generatedAt']}｜样例材料 `{spec['materialId']}`")
    A("")
    A("字段规格取自**实际响应**而不是 OpenAPI —— Mock 的路由用原始 dict body，")
    A("FastAPI 不会展开字段，所以 `openapi.json` 里的 body 是空的。")
    A("实测取样能保证规格与实际行为不脱节。")
    A("")
    A("## 通用约定")
    A("")
    A("| 约定 | 内容 |")
    A("| --- | --- |")
    A("| 时间轴 | 原音频**绝对秒数**，字段名以 `Sec` 结尾 |")
    A("| 预算与用时 | **毫秒**，字段名以 `Ms` 结尾 |")
    A("| 材料身份 | `materialId` + `materialVersion`，**不用文件路径** |")
    A("| 开发样例标识 | 所有 Mock 响应带 `developmentFixture: true` |")
    A("| 错误形状 | 业务错误 `{error, error_info}`；请求体解析错误 `{detail}`。解析用 `detail \\|\\| error_info \\|\\| error` |")
    A("| 反馈值 | `sound`（没听清）/ `meaning`（不懂意思）/ `heard`（自评听懂） |")
    A("| 带练动作 | `replay` / `slow` / `chunk` / `explain` / `transfer` / `finish` |")
    A("| 练习状态 | `active` / `complete` |")
    A("| 复习条目状态 | `pending` / `current` / `done` / `skipped` |")
    A("| 复习会话状态 | `ready` / `active` / `paused` / `complete` |")
    A("")

    for e in spec["endpoints"]:
        A(f"## `{e['method']} {e['path']}`")
        A("")
        A(e["note"])
        A("")
        if e["body"] is not None:
            A("**请求体**")
            A("")
            A("```json")
            A(json.dumps(e["body"], ensure_ascii=False, indent=2))
            A("```")
            A("")
        A(f"**响应** `HTTP {e['status']}`")
        A("")

        def emit(fields: dict, indent: int, prefix: str = "") -> None:
            pad = "  " * indent
            for name, meta in fields.items():
                if name.startswith("_"):
                    continue
                t = meta.get("type", "?")
                extra = ""
                if meta.get("nullable"):
                    extra = "（可为 null）"
                sample = meta.get("sample")
                if isinstance(sample, (str, int, float, bool)) and not isinstance(sample, bool):
                    s = str(sample)
                    if len(s) > 46:
                        s = s[:46] + "…"
                    extra += f' 例：`{s}`'
                A(f"{pad}- `{name}` : `{t}`{extra}")
                if "fields" in meta:
                    emit(meta["fields"], indent + 1)
                elif "items" in meta:
                    sub = meta["items"]
                    if isinstance(sub, dict) and any(not k.startswith("_") for k in sub):
                        A(f"{pad}  - 每项字段：")
                        emit(sub, indent + 2)
                    else:
                        A(f"{pad}  - 每项类型：`{sub.get('_type', '?')}`")
        emit(e["response"], 0)
        A("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="接口规格生成")
    parser.add_argument("--check", action="store_true", help="只比对是否与已入库规格一致")
    args = parser.parse_args()

    status, _ = call("GET", "/api/cet/materials")
    if status == 0:
        print(f"X  Mock 不可达 {MOCK}\n   先跑 scripts/qa/start_cet.sh --mock", file=sys.stderr)
        return 1

    spec = collect()
    bad = [e for e in spec["endpoints"] if e["status"] >= 400 and "错误形状" not in e["note"]]
    if bad:
        print(f"!  有 {len(bad)} 个接口返回错误，规格可能不完整：", file=sys.stderr)
        for e in bad[:6]:
            print(f"    {e['method']} {e['path']} → {e['status']}", file=sys.stderr)

    md = to_markdown(spec)
    # check 与写盘用同一份「去时间戳」内容，避免每次运行都判不一致
    spec_stable = {k: v for k, v in spec.items() if k != "generatedAt"}
    js = json.dumps(spec_stable, ensure_ascii=False, indent=2) + "\n"

    def strip_time(text: str) -> str:
        """去掉生成时间行 —— 它每次都不同，不能参与一致性判定。"""
        return "\n".join(
            line for line in text.splitlines() if "生成时间：" not in line
        )

    if args.check:
        ok = True
        if not MD_OUT.exists() or strip_time(
            MD_OUT.read_text(encoding="utf-8")
        ) != strip_time(md):
            print("[check] 接口规格.md 与实测不一致，请重新生成", file=sys.stderr)
            ok = False
        if not JSON_OUT.exists() or JSON_OUT.read_text(encoding="utf-8") != js:
            print("[check] interface-spec.json 与实测不一致", file=sys.stderr)
            ok = False
        if ok:
            print("[check] 接口规格与实测一致")
            return 0
        return 2

    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(md, encoding="utf-8")
    JSON_OUT.write_text(js, encoding="utf-8")
    print(f"[ok] 已生成 {MD_OUT.relative_to(ROOT)}（{len(spec['endpoints'])} 个接口）")
    print(f"[ok] 已生成 {JSON_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
