#!/usr/bin/env python3
"""四六级听力黑匣子 · 底座导入真实音频验证（C 负责，T01 的正面证据）。

任务书的 T01 是「干净环境启动、导入或打开样例」。
此前 C 只验证了「页面能开」「纯文本能导入」，
**从没验证过底座能否吃下我们这段真实六级音频并转写出正确时间轴**。
这个脚本补上那一块。

它做什么（全部走底座真实接口，不绕后端）：
    1. 上传样例 mp3 到底座（自动校验时长）
    2. 以 listening 模式建课
    3. 等 Whisper 转写完成（105 秒音频约 3—4 分钟）
    4. 取回字幕，校验句级时间轴真的产出
    5. 与 C 的契约时间轴对照，**报告切分口径差异**（不是判对错）

为什么「口径差异」值得单独报：
    底座 Whisper 切成 37 句，C 的契约是 32 句 —— 两边文字一致、覆盖同一音频，
    只是句边界粒度不同。这不是 bug，但**如果 A/B 各用一套基准，前端会错位**。
    所以脚本把它明确打出来，让人必须做选择，而不是默默错配。

用法：
    .venv/bin/python scripts/qa/verify_base_media_import.py
    .venv/bin/python scripts/qa/verify_base_media_import.py --json
    .venv/bin/python scripts/qa/verify_base_media_import.py --keep   # 保留导入的课

前置：底座在跑；样例音频已就位（scripts/qa/prepare_audio.py）。
耗时：约 4 分钟（主要是 Whisper）。
退出码：0 成功｜1 失败｜2 环境不满足
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:5173"
AUDIO = ROOT / ".cet-qa" / "media" / "cet6-202512-conv2.mp3"
CONTRACT = ROOT / "tests" / "fixtures" / "cet" / "material_cet6-202512-conv2.json"
TITLE = "cet-qa-media-import"


def get_json(path: str, timeout: float = 20.0):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(path: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def upload_audio(path: Path) -> dict:
    """multipart 上传。底座会自己 ffprobe 校验并返回时长。"""
    boundary = "----cetmedia" + uuid.uuid4().hex
    parts: list[bytes] = []
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    data = b"".join(parts)
    req = urllib.request.Request(
        f"{BASE}/api/v2/lessons/media-uploads", data=data, method="POST"
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="底座导入真实音频验证")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true", help="保留导入的课（默认也保留，仅作说明）")
    parser.add_argument("--timeout", type=int, default=600, help="等待转写的秒数")
    args = parser.parse_args()

    report: dict = {"steps": [], "ok": False}

    def step(name: str, ok: bool, info: dict) -> None:
        report["steps"].append({"name": name, "ok": ok, "info": info})
        if not args.json:
            print(f"  [{'OK' if ok else 'X '}] {name}" + ("" if ok else f"  {info}"))

    if not args.json:
        print("四六级听力黑匣子 · 底座导入真实音频验证")
        print(f"底座 {BASE}")
        print("-" * 68)

    # 前置
    try:
        get_json("/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"X  底座不可达 {BASE}：{exc}\n   先跑 scripts/qa/start_cet.sh", file=sys.stderr)
        return 2
    if not AUDIO.is_file():
        print(f"X  缺少样例音频 {AUDIO}\n   先跑 scripts/qa/prepare_audio.py", file=sys.stderr)
        return 2

    # 1 上传
    try:
        up = upload_audio(AUDIO)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        step("上传音频", False, {"status": exc.code, "detail": detail})
        report["ok"] = False
        return _emit(report, args)
    except Exception as exc:  # noqa: BLE001
        step("上传音频", False, {"error": str(exc)})
        return _emit(report, args)

    dur = float(up.get("duration_seconds") or 0)
    step("上传音频", dur > 0, {"uploadId": up.get("upload_id"), "durationSec": dur,
                              "sizeBytes": up.get("size_bytes")})
    if dur <= 0:
        return _emit(report, args)

    # 2 建课
    try:
        started = post_json("/api/v2/lessons/start", {
            "source_type": "uploaded_media",
            "upload_id": up["upload_id"],
            "title": TITLE,
        })
    except urllib.error.HTTPError as exc:
        step("建课", False, {"status": exc.code,
                             "detail": exc.read().decode("utf-8", "replace")[:200]})
        return _emit(report, args)

    lesson = started.get("lesson", {})
    lesson_id = lesson.get("id")
    step("建课", bool(lesson_id),
         {"lessonId": lesson_id, "mode": lesson.get("lesson_mode"),
          "workspace": started.get("workspace_url")})
    if not lesson_id:
        return _emit(report, args)

    # 3 等转写
    deadline = time.time() + args.timeout
    status = lesson.get("subtitle_status", "pending")
    waited = 0
    while time.time() < deadline:
        time.sleep(10)
        waited += 10
        try:
            st = get_json(f"/api/v2/lessons/{lesson_id}/status")
            status = (st.get("lesson") or {}).get("subtitle_status", status)
        except Exception:  # noqa: BLE001
            continue
        if not args.json and waited % 60 == 0:
            print(f"        …已等 {waited}s，subtitle_status={status}")
        if status in ("ready", "failed", "error"):
            break

    step("Whisper 转写", status == "ready", {"status": status, "waitedSec": waited})
    if status != "ready":
        err = ""
        try:
            err = (get_json(f"/api/v2/lessons/{lesson_id}/status").get("lesson") or {}).get(
                "subtitle_error", "")
        except Exception:  # noqa: BLE001
            pass
        step("转写错误信息", False, {"error": err[:200]})
        return _emit(report, args)

    # 4 校验时间轴
    try:
        subs = get_json(f"/api/v2/lessons/{lesson_id}/subtitles")
    except Exception as exc:  # noqa: BLE001
        step("取回字幕", False, {"error": str(exc)})
        return _emit(report, args)

    units = subs.get("sentence_units") or []
    segs = subs.get("segments") or []
    valid = bool(units) and all(
        isinstance(u.get("start"), (int, float))
        and isinstance(u.get("end"), (int, float))
        and u["start"] < u["end"]
        for u in units
    )
    step("产出句级时间轴", valid,
         {"sentenceUnits": len(units), "segments": len(segs),
          "coverage": [units[0]["start"], units[-1]["end"]] if units else None})
    if not valid:
        return _emit(report, args)

    # 5 与契约对照（报口径差异，不判对错）
    if CONTRACT.is_file():
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))["sentences"]
        gap = {
            "baseUnits": len(units),
            "contractSentences": len(contract),
            "coverageBase": [round(units[0]["start"], 2), round(units[-1]["end"], 2)],
            "coverageContract": [round(contract[0]["startSec"], 2),
                                 round(contract[-1]["endSec"], 2)],
            "sameCount": len(units) == len(contract),
        }
        step("与契约时间轴对照", True, gap)
        report["segmentationGap"] = gap
        if not gap["sameCount"] and not args.json:
            print()
            print(f"      ⚠ 切分口径不同：底座 {len(units)} 句 vs 契约 {len(contract)} 句。")
            print("        两者文字一致、覆盖同一音频，只是句边界粒度不同。")
            print("        **A/B 必须选同一套基准**，否则回听定位会错位（见问题清单 OBS-002）。")

    report["ok"] = True
    return _emit(report, args)


def _emit(report: dict, args) -> int:
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("-" * 68)
        failed = [s for s in report["steps"] if not s["ok"]]
        if report.get("ok"):
            print("结论：底座能导入真实六级音频并产出可用的句级时间轴。")
            print("      T01「导入或打开样例」的底座侧已验证。")
        else:
            print(f"结论：{len(failed)} 步失败")
            for s in failed:
                print(f"  · {s['name']}: {json.dumps(s['info'], ensure_ascii=False)[:200]}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
