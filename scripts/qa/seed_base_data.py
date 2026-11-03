#!/usr/bin/env python3
"""四六级听力黑匣子 · 底座数据预置（C 负责）。

为什么需要它：
    底座默认是干净的（数据库 47 张表但 0 行）。要做「有内容」的 E2E ——
    比如验证工作台能打开课程、词汇本不为空、句子标记能落库 ——
    必须先把数据导进去。BUG-003 查清后确认：导入链路本身正常，
    只是没人喂过数据。这个脚本把「喂数据」变成可重复的一步。

它做什么：
    1. 按底座真实的导入接口（不是直接写库）建一节 reading 课
    2. 轮询任务到 done
    3. 校验内容真的入库（lessons 列表 + blocks 正文）
    4. 可选：加一条句子标记，供之后验证持久化

为什么走接口而不是直接写库：
    直接写库只能证明「我能插数据」，证明不了「底座的导入链路是通的」。
    任务书要验的是后者。而且绕过接口写库会漏掉迁移与业务校验。

用法：
    .venv/bin/python scripts/qa/seed_base_data.py
    .venv/bin/python scripts/qa/seed_base_data.py --check      # 只检查有没有数据，不导入
    .venv/bin/python scripts/qa/seed_base_data.py --force      # 已有同名课也再导一份

前置：底座在跑（scripts/qa/start_cet.sh）。**不要用 AI 密钥**——纯文本导入不需要模型。
退出码：0 成功｜1 失败
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5173"
ROOT = Path(__file__).resolve().parents[2]
SEED_TEXT = ROOT / ".cet-qa" / "seed" / "sample-reading.txt"
SEED_MARK_FILENAME_HINT = "sample-reading"

SAMPLE = """The university library is trying something new this semester. Students can now borrow a laptop for up to three hours at a time. The programme started with just twenty devices and has already expanded twice. Library staff say demand has been steady since the first week. Most students use the laptops for group projects in the late afternoon. The library plans to add another forty machines before the end of term.
"""


def req(method: str, path: str, *, data: bytes | None = None, headers: dict | None = None):
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def multipart(field: str, filename: str, content: bytes, extra: dict[str, str]) -> tuple[bytes, dict]:
    boundary = "----cetqaboundary7f3a"
    parts: list[bytes] = []
    for k, v in extra.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: text/plain\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def service_alive() -> bool:
    try:
        status, _ = req("GET", "/health")
        return status == 200
    except Exception:  # noqa: BLE001
        return False


def delete_lesson(lesson_id) -> bool:
    try:
        status, _ = req("DELETE", f"/api/v2/lessons/library/{lesson_id}")
        return status in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def clean_lessons(all_: bool) -> None:
    """把课程库收敛到已知状态。

    为什么需要：C 调试时会反复导入同一段音频（验证导入链路、验证 Whisper），
    库里会积累多节同名课，导致「第一节是哪门课」不确定，
    依赖排序的检查就会飘。

    --clean     保留最早一节预置课，删掉重复的
    --clean-all 全删（会失去做有内容 E2E 的数据，需重新导入）
    """
    lessons = list_lessons()
    if not lessons:
        print("OK  课程库已为空")
        return
    seeded = [l for l in lessons if SEED_MARK_FILENAME_HINT in str(l.get("title") or "")]
    if all_:
        targets = lessons
        print(f"清理：删除全部 {len(targets)} 节课")
    else:
        # 只处理预置课 + 重复的同名导入课，保留最早一节预置课
        seeded.sort(key=lambda l: l.get("id") or 0)
        targets = seeded[1:] if len(seeded) > 1 else []
        dup_media = {}
        for l in lessons:
            t = str(l.get("title") or "")
            key = (t, l.get("source_type"))
            dup_media.setdefault(key, []).append(l)
        for key, group in dup_media.items():
            group.sort(key=lambda l: l.get("id") or 0)
            targets.extend(group[1:])
        if not targets:
            print("OK  无重复课程需要清理")
            return
        print(f"清理：删除 {len(targets)} 节重复课（保留最早各一节）")
    for l in targets:
        lid = l.get("id")
        ok = delete_lesson(lid)
        print(f"  {'OK ' if ok else 'X  '} 删除 id={lid} {str(l.get('title'))[:28]} ({l.get('source_type')})")
    print(f"OK  清理完成，剩余 {len(list_lessons())} 节")


def list_lessons() -> list[dict]:
    status, body = req("GET", "/api/v2/lessons/library")
    if status != 200 or not isinstance(body, dict):
        return []
    return body.get("lessons") or []


def main() -> int:
    parser = argparse.ArgumentParser(description="底座数据预置")
    parser.add_argument("--check", action="store_true", help="只检查，不导入")
    parser.add_argument("--force", action="store_true", help="已有同名课也再导一份")
    parser.add_argument("--clean", action="store_true",
                        help="先删除重复的预置课（保留最早一节），把库收敛到已知状态")
    parser.add_argument("--clean-all", action="store_true",
                        help="删除全部预置课与测试导入课，回到空库")
    parser.add_argument("--timeout", type=int, default=90, help="等待导入完成的秒数")
    args = parser.parse_args()

    print("四六级听力黑匣子 · 底座数据预置")
    print(f"目标 {BASE}")
    print("-" * 60)

    if not service_alive():
        print(f"X  底座未运行或不可达：{BASE}\n   先启动：scripts/qa/start_cet.sh")
        return 1
    print("OK  底座可达")

    if args.clean or args.clean_all:
        clean_lessons(args.clean_all)
        if args.clean_all:
            print("（库已清空；不带 --check 再跑一次可重新导入预置课）")
            return 0

    lessons = list_lessons()
    print(f"OK  当前课程数：{len(lessons)}")
    existing = [l for l in lessons if SEED_MARK_FILENAME_HINT in str(l.get("title") or "")]
    if existing:
        print(f"     已有预置课：{[l.get('id') for l in existing]}（标题含 {SEED_MARK_FILENAME_HINT}）")

    if args.check:
        if lessons:
            print("\n结论：已有数据，可直接做有内容的 E2E")
            return 0
        print("\n结论：库为空。跑一次不带 --check 即可预置。")
        return 0

    if existing and not args.force:
        print("\n已有预置课，跳过导入（要再导一份用 --force）")
    else:
        SEED_TEXT.parent.mkdir(parents=True, exist_ok=True)
        SEED_TEXT.write_text(SAMPLE, encoding="utf-8")
        body, hdrs = multipart(
            "file", "sample-reading.txt", SAMPLE.encode("utf-8"), {"tts": "false"}
        )
        status, resp = req("POST", "/api/v2/lessons/reading/upload", data=body, headers=hdrs)
        if status != 200 or not isinstance(resp, dict):
            print(f"X  导入请求失败：HTTP {status} {resp}")
            return 1
        job_id = resp.get("job_id")
        print(f"OK  导入已排队，job_id={job_id}")

        deadline = time.time() + args.timeout
        stage = None
        while time.time() < deadline:
            time.sleep(1.5)
            st, job = req("GET", f"/api/v2/lessons/reading/upload-status/{job_id}")
            if st != 200 or not isinstance(job, dict):
                print(f"X  查询任务失败：HTTP {st} {job}")
                return 1
            stage = job.get("stage")
            if stage == "done":
                print(f"OK  导入完成：{job.get('message')} → {job.get('workspace_url')}")
                break
            if stage in ("failed", "error"):
                print(f"X  导入失败：{job.get('error') or job}")
                return 1
        else:
            print(f"X  导入超时（>{args.timeout}s），最后阶段 {stage}")
            return 1

    # 校验内容真的入库 —— 只信「接口能读回来」，不信「任务说完成」
    lessons = list_lessons()
    seeded = [l for l in lessons if SEED_MARK_FILENAME_HINT in str(l.get("title") or "")]
    print(f"OK  课程库现有 {len(lessons)} 节，其中预置课 {len(seeded)} 节")
    if not seeded:
        print("X  导入说完成但课程库里找不到，判失败")
        return 1

    lesson_id = seeded[-1].get("id")
    st, detail = req("GET", f"/api/v2/lessons/{lesson_id}/reading")
    if st != 200 or not isinstance(detail, dict):
        print(f"X  读不回课程正文：HTTP {st} {detail}")
        return 1
    blocks = detail.get("blocks") or []
    joined = " ".join(str(b.get("text") or "") for b in blocks)
    ok_text = "university library" in joined.lower()
    print(f"{'OK' if ok_text else 'X '} 正文可读回：{len(blocks)} 个块，"
          f"{len(joined)} 字符{'，内容匹配' if ok_text else '，内容不匹配'}")
    if not ok_text:
        return 1

    # 句子标记：验证底座既有的句子级持久化真的可用（BUG-003 复查的一部分）
    mark_status, mark_resp = req(
        "POST",
        "/api/sentence-mark",
        data=json.dumps(
            {"lesson_filename": "sample-reading.txt", "sentence_idx": 1, "marked": True}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    if mark_status == 200:
        st2, marks = req("GET", "/api/sentence-marks/sample-reading.txt")
        print(f"OK  句子标记可写可读：{marks}")
    else:
        print(f"!   句子标记接口返回 {mark_status}（{mark_resp}）；不影响课程数据")

    print("-" * 60)
    print("结论：底座数据已预置，可做有内容的 E2E")
    print(f"     课程 id={lesson_id}，工作台 /workspace/{lesson_id}")
    print("     注意：纯文本导入没有句级时间轴（subtitle_status=pending 属正常）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
